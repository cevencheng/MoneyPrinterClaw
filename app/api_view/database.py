"""
SQLite 数据库管理模块

负责：
- SQLAlchemy 异步引擎和会话管理
- 数据表模型定义（会话元数据、业务消息）
- 自动建表（首次启动时）

数据分层（与 LangGraph Checkpointer 解耦）：
- sessions：会话元数据（标题/时间）。
- messages：业务消息真相源（user/assistant 对话气泡，含 tool-call 卡片）。
  reload 由此读取；checkpointer 仅作状态机断点续传。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent.config import settings


# ============================================================
# 数据表模型
# ============================================================

class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类"""
    pass


class Session(Base):
    """会话表 — 记录每次对话的元信息"""
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class Message(Base):
    """业务消息表 — 对话气泡的真相源（与前端 BackendMessage 对齐）。

    parts 字段存 JSON 数组，元素形如：
      {"type": "text", "text": "..."}
      {"type": "tool-call", "toolCallId": "...", "toolName": "...", "args": {...}, "result": ...?}

    seq 是会话内严格递增序号（INSERT 时取 max+1）；UNIQUE(session_id, seq) 让迁移脚本可重入。
    """
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID hex
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" / "assistant"
    parts: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    created_at: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),
        Index("ix_messages_session_seq", "session_id", "seq"),
    )


# ============================================================
# 引擎与会话工厂
# ============================================================

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_db_dir() -> None:
    """确保数据库文件所在目录存在"""
    db_path = Path(settings.sqlite_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)


async def init_db() -> None:
    """
    初始化数据库引擎并创建表

    在 FastAPI lifespan 启动阶段调用。
    """
    global _engine, _session_factory

    _ensure_db_dir()

    _engine = create_async_engine(
        f"sqlite+aiosqlite:///{settings.sqlite_db_path}",
        echo=False,
    )

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # 自动建表（含一次性升级：旧 messages 表 schema 与新 schema 不兼容,直接 DROP 重建）
    def _upgrade_messages_schema(sync_conn) -> None:
        from sqlalchemy import inspect

        insp = inspect(sync_conn)
        if "messages" not in insp.get_table_names():
            return  # 全新库,create_all 直接建新 schema
        cols = {c["name"] for c in insp.get_columns("messages")}
        if "seq" not in cols or "parts" not in cols:
            # 旧 schema（id INTEGER autoincrement,content TEXT）→ 丢弃重建
            # 旧表本来就 dead（0 行）,无数据损失
            sync_conn.exec_driver_sql("DROP TABLE messages")

    async with _engine.begin() as conn:
        await conn.run_sync(_upgrade_messages_schema)
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭数据库引擎，在 FastAPI lifespan 关闭阶段调用"""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取会话工厂"""
    if _session_factory is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话（用于 FastAPI 依赖注入）

    用法：
        @router.get("/")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session
