"""
业务消息存储 —— 真相源（reload UI 用,与 LangGraph Checkpointer 解耦）。

数据流：
- chat.py SSE 流末调用 MessageBuilder：把流过程里的 SSE 事件累积成 BackendMessage 列表 → 写库
- history.py 优先从此读消息;回退 checkpointer（兼容期）

消息结构与前端 BackendMessage 对齐：
    { id, role: "user"|"assistant", content: Part[] }
    Part: {type:"text", text} | {type:"tool-call", toolCallId, toolName, args, result?}

并发安全（写入）：
- 老实现「SELECT max(seq) → INSERT seq」两步在 SQLAlchemy + aiosqlite 下有缺陷：commit
  后下一个 session 拿到的 connection 仍持旧快照,SELECT max 读到 NULL → 仍算 seq=0 →
  UNIQUE 冲突 → assistant 业务消息丢失。即便加 asyncio.Lock 串行也救不了（实测）。
- 现实现：把 SELECT 子查询直接嵌进 INSERT VALUES 单条 SQL,SQLite 引擎层原子执行：
      INSERT INTO messages(...) VALUES (?, ?, (SELECT IFNULL(MAX(seq),-1)+1 FROM ...), ?, ?, ?)
  并发到达时 SQLite 写锁让两个 INSERT 串行,第二个执行子查询时已能看见第一个的 commit。
  完全无 race,无需 lock,无需 retry。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api_view.database import Message, get_session_factory

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 单条 SQL：把 SELECT MAX(seq) 子查询嵌进 INSERT VALUES,SQLite 引擎层原子执行
# 规避「session-level read snapshot 过期」+「并发 race」两个潜在问题
_INSERT_MESSAGE_SQL = text(
    """
    INSERT INTO messages (id, session_id, seq, role, parts, created_at)
    VALUES (
        :id,
        :session_id,
        (SELECT IFNULL(MAX(seq), -1) + 1 FROM messages WHERE session_id = :session_id),
        :role,
        :parts,
        :created_at
    )
    """
)


# ─────────────────────────── 读取 ────────────────────────────


async def list_messages(session_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    """按 seq 升序读取会话的全部业务消息（reload 用）。"""
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.seq.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "role": r.role,
            "content": json.loads(r.parts) if r.parts else [],
        }
        for r in rows
    ]


async def has_messages(session_id: str, db: AsyncSession) -> bool:
    """判断会话在业务消息表里是否有任何记录（用于 reload fallback 决策）。"""
    from sqlalchemy import func

    stmt = select(func.count()).select_from(Message).where(Message.session_id == session_id)
    n = (await db.execute(stmt)).scalar() or 0
    return n > 0


# ─────────────────────────── 写入 ────────────────────────────


async def _append_message(
    session_id: str, role: str, parts: list[dict[str, Any]], db: AsyncSession
) -> str:
    """append 一条 message,返回其 id。

    并发/快照安全：单条 SQL 内嵌 `(SELECT MAX(seq)+1)`,由 SQLite 引擎在写锁内
    原子执行。两个并发 INSERT → 串行,第二个执行子查询时看到第一个的 commit,
    自动拿到 seq=N+1。
    """
    msg_id = _new_id()
    await db.execute(
        _INSERT_MESSAGE_SQL,
        {
            "id": msg_id,
            "session_id": session_id,
            "role": role,
            "parts": json.dumps(parts, ensure_ascii=False),
            "created_at": _now(),
        },
    )
    await db.commit()
    return msg_id


async def append_user_message(session_id: str, text: str) -> str:
    """append 一条 user 消息（仅纯文本）。"""
    factory = get_session_factory()
    async with factory() as db:
        return await _append_message(
            session_id, "user", [{"type": "text", "text": text}], db
        )


async def append_assistant_message(session_id: str, parts: list[dict[str, Any]]) -> str:
    """append 一条 assistant 消息（含混合 text/tool-call parts）。"""
    if not parts:
        return ""  # 空消息不写
    factory = get_session_factory()
    async with factory() as db:
        return await _append_message(session_id, "assistant", parts, db)


async def update_last_assistant_tool_result(
    session_id: str, tool_call_id: str, result: Any
) -> bool:
    """把 session 中最后一条 assistant 消息里指定 toolCallId 的 part.result 原地更新。

    用于 resume 流程：用户提交 review_video_plan → 把那张评审卡的 result 写实。
    返回 True 表示有改动,False 表示找不到目标卡片。
    """
    factory = get_session_factory()
    async with factory() as db:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id, Message.role == "assistant")
            .order_by(Message.seq.desc())
            .limit(1)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        try:
            parts = json.loads(row.parts) if row.parts else []
        except json.JSONDecodeError:
            return False
        changed = False
        for p in parts:
            if p.get("type") == "tool-call" and p.get("toolCallId") == tool_call_id:
                p["result"] = result
                changed = True
                break
        if not changed:
            return False
        row.parts = json.dumps(parts, ensure_ascii=False)
        await db.commit()
        return True


async def get_last_assistant(session_id: str) -> dict[str, Any] | None:
    """读取 session 末条 assistant 消息。返回 {id, parts} 或 None。

    用于 /continue：续跑前先看 DB 里有没有未完成的 assistant 行（含 render_video tool-call
    无 result 的那种）。如果有,本次续跑不再 INSERT 新行,而是 UPDATE 原行 → 避免视觉上
    出现「DB 旧卡 + 续跑新卡」的两条 bubble。
    """
    factory = get_session_factory()
    async with factory() as db:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id, Message.role == "assistant")
            .order_by(Message.seq.desc())
            .limit(1)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        try:
            parts = json.loads(row.parts) if row.parts else []
        except json.JSONDecodeError:
            parts = []
        return {"id": row.id, "parts": parts}


async def replace_assistant_message(message_id: str, parts: list[dict[str, Any]]) -> bool:
    """按 message id 原地替换 assistant 消息的 parts（不变 seq、created_at）。

    用于 /continue 续跑结束后 flush：用更新后的 builder.build() 覆盖原行，
    而非 INSERT 新行。
    """
    if not message_id:
        return False
    factory = get_session_factory()
    async with factory() as db:
        stmt = select(Message).where(Message.id == message_id)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        row.parts = json.dumps(parts, ensure_ascii=False)
        await db.commit()
        return True


# ─────────────────────────── SSE → parts 累积器 ────────────────────────────


class AssistantMessageBuilder:
    """累积一次 SSE 流的 assistant parts,流末一次性写库。

    有序 parts 队列：text 与 tool-call 按到达时间穿插排列（而非 text 全在前）。
    批量任务里 agent 有两段文本（开头说明 + 结尾总结）,中间隔着工具卡 ——
    穿插排列让结尾总结显示在工具卡之后,而不是被拼到最前。

    - delta：累积到 _pending_text,遇到 tool_call 或 build 时封段入队（避免碎成千百个 text part）
    - tool_call：先封当前 _pending_text,再原位更新（同 id）或追加（新 id）tool-call part
    - tool_result：按 _tool_index 原位更新 result,不挪位
    """

    def __init__(self) -> None:
        self._parts: list[dict[str, Any]] = []
        self._tool_index: dict[str, int] = {}  # toolCallId → 在 _parts 中的下标
        self._pending_text: str = ""

    def seed(self, parts: list[dict[str, Any]]) -> None:
        """从既有 parts（如 DB 里加载的旧 assistant 消息）按原顺序填充内部状态。

        用于 /continue：把上一次中断时已写入 DB 的内容预加载进来,后续事件 update 在此之上,
        flush 时 REPLACE 同一行（避免 DB 多出一条 assistant 行）。按原顺序入队,
        text part 直接保留为独立 part（不同时间段的文本不合并）。
        """
        for p in parts or []:
            ptype = p.get("type")
            if ptype == "text":
                t = p.get("text", "") or ""
                if t:
                    self._parts.append({"type": "text", "text": t})
            elif ptype == "tool-call":
                tid = p.get("toolCallId") or ""
                if not tid:
                    continue
                self._tool_index[tid] = len(self._parts)
                self._parts.append({
                    "type": "tool-call",
                    "toolCallId": tid,
                    "toolName": p.get("toolName", ""),
                    "args": p.get("args", {}) or {},
                    **({"result": p["result"]} if "result" in p else {}),
                })

    def add_delta(self, text: str) -> None:
        if text:
            self._pending_text += text

    def _flush_pending_text(self) -> None:
        """把累积的 _pending_text 封成 text part 入队（非空才入）,清空累积。"""
        if self._pending_text:
            self._parts.append({"type": "text", "text": self._pending_text})
            self._pending_text = ""

    def add_tool_call(
        self, tool_id: str, name: str, args: dict[str, Any] | None
    ) -> None:
        # 先封当前 text 段入队,保证 text 在此 tool-call 之前
        self._flush_pending_text()
        idx = self._tool_index.get(tool_id)
        if idx is None:
            # 新 tool-call：追加,记录位置
            self._tool_index[tool_id] = len(self._parts)
            self._parts.append({
                "type": "tool-call",
                "toolCallId": tool_id,
                "toolName": name,
                "args": args or {},
            })
        else:
            # 同 id 后续事件：原位更新 args/toolName（progress 推进进度）,不挪位
            existing = self._parts[idx]
            existing["toolName"] = name or existing.get("toolName", "")
            if args:
                existing["args"] = args

    def add_tool_result(self, tool_id: str, result: Any) -> None:
        idx = self._tool_index.get(tool_id)
        if idx is None:
            # 没见过 start 但来了 result —— 极少见,补一个空 tool-call 兜底（同原逻辑）
            self._tool_index[tool_id] = len(self._parts)
            self._parts.append({
                "type": "tool-call",
                "toolCallId": tool_id,
                "toolName": "",
                "args": {},
                "result": result,
            })
        else:
            self._parts[idx]["result"] = result

    def build(self) -> list[dict[str, Any]]:
        """生成 parts：text 与 tool-call 按到达时间穿插排列。末尾未封的 text 一并 flush。"""
        self._flush_pending_text()
        return list(self._parts)

    def is_empty(self) -> bool:
        return not self._parts and not self._pending_text
