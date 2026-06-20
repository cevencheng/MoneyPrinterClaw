"""
会话管理 API

提供多会话 CRUD 接口：
- GET    /api/threads         — 列出所有会话
- POST   /api/threads         — 创建新会话
- GET    /api/threads/{id}    — 获取单个会话
- PATCH  /api/threads/{id}    — 重命名会话
- DELETE /api/threads/{id}    — 删除会话及其消息
- POST   /api/threads/{id}/title — 自动生成标题
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api_view.agent_runtime import delete_thread_state
from api_view.database import Message, Session, get_db

router = APIRouter()


def _now() -> str:
    """返回 ISO 格式的当前时间"""
    return datetime.now(timezone.utc).isoformat()


@router.get("")
async def list_threads(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """列出会话（按更新时间倒序，分页）。

    前端按 `?limit=&offset=` 翻页；返回数量 < limit 即视为到底。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    stmt = (
        select(Session)
        .order_by(Session.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    sessions = (await db.execute(stmt)).scalars().all()

    return [
        {
            "id": s.session_id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in sessions
    ]


@router.post("")
async def create_thread(request: Request, db: AsyncSession = Depends(get_db)):
    """创建新会话"""
    body = {} if request.headers.get("content-length") == "0" else await request.json()
    thread_id = body.get("localId", body.get("id", ""))

    if not thread_id:
        import uuid
        thread_id = uuid.uuid4().hex  # 完整 32 位 hex（122 位熵，SaaS 多租户抗冲突）

    now = _now()
    session = Session(
        session_id=thread_id,
        title="新对话",
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.commit()

    return {"id": thread_id, "title": "新对话"}


@router.get("/{thread_id}")
async def get_thread(thread_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个会话信息"""
    stmt = select(Session).where(Session.session_id == thread_id)
    s = (await db.execute(stmt)).scalar_one_or_none()

    if not s:
        return {"id": thread_id, "title": "新对话", "archived": False}

    return {
        "id": s.session_id,
        "title": s.title,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "archived": False,
    }


@router.patch("/{thread_id}")
async def rename_thread(thread_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """重命名会话"""
    body = await request.json()
    new_title = body.get("title", "")

    if new_title:
        await db.execute(
            update(Session)
            .where(Session.session_id == thread_id)
            .values(title=new_title, updated_at=_now())
        )
        await db.commit()

    return {"id": thread_id, "title": new_title}


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, db: AsyncSession = Depends(get_db)):
    """彻底删除会话相关全部数据：sessions + messages + checkpoints + writes 四张表。

    防止僵尸会话残留：原先此端点只删 sessions,messages/checkpoints/writes 都成孤儿。
    最致命的副作用是 checkpoints 残留 → 哪怕 sessions 删了,只要 thread_id 仍能传到
    /api/chat/continue,后端从 checkpointer 读得到状态就会接着续跑（如 stuck 的会话
    自动续跑死循环）。
    """
    # 1. 业务层（同一个 db session 里两表一起 commit）
    await db.execute(delete(Message).where(Message.session_id == thread_id))
    await db.execute(delete(Session).where(Session.session_id == thread_id))
    await db.commit()

    # 2. 状态机层（checkpoints + writes 全 ns 全删；走 agent_runtime 的独立 conn）
    state_stats = await delete_thread_state(thread_id)

    return {"status": "deleted", "id": thread_id, **state_stats}


@router.post("/{thread_id}/title")
async def generate_title(thread_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """根据首条消息自动生成会话标题"""
    body = await request.json()
    messages = body.get("messages", [])

    # 取第一条用户消息
    user_msg = ""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
        if msg.get("role") == "user" and content:
            user_msg = content
            break

    # 简单截取前 20 字符作为标题（避免额外 LLM 调用）
    title = user_msg[:20].strip() + ("..." if len(user_msg) > 20 else "")
    if not title:
        title = "新对话"

    await db.execute(
        update(Session)
        .where(Session.session_id == thread_id)
        .values(title=title, updated_at=_now())
    )
    await db.commit()

    return {"title": title}
