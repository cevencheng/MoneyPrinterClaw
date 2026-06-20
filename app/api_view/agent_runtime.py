"""
Agent Runtime 单例管理

隔离 LangGraph Agent 的编译与获取，避免 api_view.web_main 与各 API 模块之间的循环导入。
负责管理 AsyncSqliteSaver 的生命周期上下文。
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent.config import settings
from agent.graph.build import build_agent

logger = logging.getLogger(__name__)

_checkpointer_context = None
_checkpointer: AsyncSqliteSaver | None = None
_agent_graph = None


async def init_agent_graph() -> None:
    """在应用启动时编译并缓存 LangGraph Agent"""
    global _checkpointer_context, _checkpointer, _agent_graph
    _checkpointer_context = AsyncSqliteSaver.from_conn_string(settings.sqlite_db_path)
    _checkpointer = await _checkpointer_context.__aenter__()
    _agent_graph = await build_agent(_checkpointer)


async def close_agent_graph() -> None:
    """关闭 Checkpointer 上下文"""
    global _checkpointer_context
    if _checkpointer_context is not None:
        await _checkpointer_context.__aexit__(None, None, None)
        _checkpointer_context = None


def get_agent_graph():
    """获取已初始化的 LangGraph Agent"""
    if _agent_graph is None:
        raise RuntimeError("Agent graph not initialized. Call init_agent_graph() first.")
    return _agent_graph


def get_checkpointer() -> AsyncSqliteSaver:
    """直接拿到 checkpointer（裁剪/迁移脚本需要直接 SQL 操作底层 conn）。"""
    if _checkpointer is None:
        raise RuntimeError("Checkpointer not initialized.")
    return _checkpointer


async def prune_checkpoints(thread_id: str, keep: int = 3) -> dict:
    """裁剪指定 thread 的 checkpoint，仅保留主图 ns 最近 keep 个 + 删全部子图 ns。

    关键事实（实测）：
    - 主图 checkpoint_ns = ''（一个 thread 一份递增序列）
    - supervisor 子图每次调用建独立 ns（"creative:<uuid>"）；执行完后状态可弃。
      一个批量会话会累积几百个 ns,占空间巨大。

    故 thread idle 时（调用方已校验）：
    - 主 ns 保留最新 keep 个 checkpoint（resume/续跑兜底）
    - 所有子图 ns 整个 thread 删除（其状态对主图是 snapshot 副本,不影响主图引用）

    Returns: {"main_deleted": N, "subgraph_deleted": M}
    """
    if keep < 1:
        keep = 1
    cp = get_checkpointer()
    conn = getattr(cp, "conn", None)
    if conn is None:
        logger.warning("[prune] checkpointer has no .conn attribute,skip")
        return {"main_deleted": 0, "subgraph_deleted": 0}

    try:
        # 1) 主 ns 最近 keep 个 checkpoint_id
        cur = await conn.execute(
            "SELECT checkpoint_id FROM checkpoints "
            "WHERE thread_id = ? AND checkpoint_ns = '' "
            "ORDER BY checkpoint_id DESC LIMIT ?",
            (thread_id, keep),
        )
        keep_ids = [r[0] for r in await cur.fetchall()]
        await cur.close()

        main_deleted = 0
        if keep_ids:
            placeholders = ",".join("?" * len(keep_ids))
            count_cur = await conn.execute(
                f"SELECT COUNT(*) FROM checkpoints "
                f"WHERE thread_id = ? AND checkpoint_ns = '' "
                f"AND checkpoint_id NOT IN ({placeholders})",
                (thread_id, *keep_ids),
            )
            main_deleted = (await count_cur.fetchone())[0]
            await count_cur.close()
            await conn.execute(
                f"DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' "
                f"AND checkpoint_id NOT IN ({placeholders})",
                (thread_id, *keep_ids),
            )
            await conn.execute(
                f"DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = '' "
                f"AND checkpoint_id NOT IN ({placeholders})",
                (thread_id, *keep_ids),
            )

        # 2) 子图 ns 全删（idle 时它们都已执行完）
        sub_count_cur = await conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ? AND checkpoint_ns != ''",
            (thread_id,),
        )
        subgraph_deleted = (await sub_count_cur.fetchone())[0]
        await sub_count_cur.close()
        await conn.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns != ''",
            (thread_id,),
        )
        await conn.execute(
            "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns != ''",
            (thread_id,),
        )

        await conn.commit()
        if main_deleted or subgraph_deleted:
            logger.info(
                "[prune] thread=%s main_deleted=%d subgraph_deleted=%d kept_main=%d",
                thread_id, main_deleted, subgraph_deleted, len(keep_ids),
            )
        return {"main_deleted": main_deleted, "subgraph_deleted": subgraph_deleted}
    except Exception as e:
        logger.exception("[prune] failed for thread=%s: %s", thread_id, e)
        return {"main_deleted": 0, "subgraph_deleted": 0}


async def delete_thread_state(thread_id: str) -> dict:
    """彻底清除指定 thread 在状态机层的全部数据（checkpoints + writes 所有 ns)。

    与 prune_checkpoints(keep>=1) 的"裁剪保留"语义不同,本函数无保留 —— 用于
    delete_thread 端点的"销毁会话"路径,确保不留状态机孤儿数据（防止僵尸会话
    通过 thread_id 续跑出来）。

    Returns: {"checkpoints_deleted": N, "writes_deleted": M}
    """
    cp = get_checkpointer()
    conn = getattr(cp, "conn", None)
    if conn is None:
        logger.warning("[delete_thread_state] checkpointer has no .conn attribute,skip")
        return {"checkpoints_deleted": 0, "writes_deleted": 0}

    try:
        cp_count_cur = await conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
        )
        n_cp = (await cp_count_cur.fetchone())[0]
        await cp_count_cur.close()

        w_count_cur = await conn.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = ?", (thread_id,)
        )
        n_w = (await w_count_cur.fetchone())[0]
        await w_count_cur.close()

        await conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        await conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        await conn.commit()

        if n_cp or n_w:
            logger.info(
                "[delete_thread_state] thread=%s checkpoints=%d writes=%d",
                thread_id, n_cp, n_w,
            )
        return {"checkpoints_deleted": n_cp, "writes_deleted": n_w}
    except Exception as e:
        logger.exception("[delete_thread_state] failed for thread=%s: %s", thread_id, e)
        return {"checkpoints_deleted": 0, "writes_deleted": 0}
