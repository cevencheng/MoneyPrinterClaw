"""
FastAPI 应用入口

负责：
- 应用生命周期管理（lifespan）：启动时初始化资源，关闭时清理
- 路由注册：挂载 API 路由
- CORS 配置：允许跨域请求
- 全局异常处理
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api_view.web_config import API_TITLE, API_VERSION, API_DESCRIPTION
from api_view.api.chat import router as chat_router
from api_view.api.history import router as history_router
from api_view.api.settings import router as settings_router
from api_view.api.skills import router as skills_router
from api_view.api.threads import router as threads_router
from api_view.database import close_db, init_db
from api_view.agent_runtime import close_agent_graph, init_agent_graph
from agent.video.bridge import tasks_root


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时：初始化数据库、预加载资源
    关闭时：关闭数据库连接、释放资源
    """
    # ---- 启动阶段 ----
    import logging
    # force=True：强制覆盖 uvicorn 自带的日志配置，确保应用层日志（agent/chat/...）能输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        force=True,
    )
    # 显式确保关键 logger 输出（防止被第三方库压到 WARNING 导致看着像卡住）
    for _n in ("agent", "api_view", "httpx", "langchain", "langgraph"):
        logging.getLogger(_n).setLevel(logging.INFO)

    # 静默 SQLA 连接池在 SSE 客户端断连时的 CancelledError 噪音：
    # 客户端中途关连接 → ASGI request scope 被 cancel → aiosqlite.close 被 cancel →
    # sqlalchemy.pool.impl.AsyncAdaptedQueuePool 用 logger.error 打 "Exception terminating
    # connection" + 完整 stack。这是清理路径的正常 cancel,功能完全无害,只是日志噪音。
    class _SilenceSQLAPoolCancel(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # ⚠️ 必须用 startswith 兜住所有子 logger（实际错误来自
            # sqlalchemy.pool.impl.AsyncAdaptedQueuePool 而非 sqlalchemy.pool 本身）
            if record.name.startswith("sqlalchemy.pool"):
                msg = record.getMessage()
                if "Exception terminating connection" in msg:
                    return False
                exc = record.exc_info
                if exc and exc[0] is not None and issubclass(exc[0], asyncio.CancelledError):
                    return False
            return True

    # ⚠️ Python logging 隐藏陷阱：filter 装在 logger 上只过滤【该 logger 直接 emit 的事件】,
    # 子 logger 的事件 propagate 到父 logger 时,沿途的 ancestor filter 不被调用。
    # 实际错误来自 sqlalchemy.pool.impl.AsyncAdaptedQueuePool（子 logger）→ 装在
    # "sqlalchemy.pool"（父 logger）上完全无效 —— 这是上一版本 bug。
    # 正解：装在 root 的所有 handler 上 —— handler filter 对【所有路由到该 handler 的事件】
    # 生效,包括从子 logger propagate 上来的。filter 自带 startswith("sqlalchemy.pool") 门控,
    # 不会误伤其它日志。
    _silencer = _SilenceSQLAPoolCancel()
    for _h in logging.getLogger().handlers:
        _h.addFilter(_silencer)
    # 同时挂在 sqlalchemy.pool 父 logger 上,防御未来若有事件直接 emit 在父 logger（保险层）
    logging.getLogger("sqlalchemy.pool").addFilter(_silencer)

    await init_db()  # 创建 SQLite 引擎 + 自动建表
    await init_agent_graph()  # 编译 LangGraph Agent

    yield  # 应用运行中

    # ---- 关闭阶段 ----
    await close_agent_graph()
    await close_db()


# 创建 FastAPI 应用实例
app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
    lifespan=lifespan,
)

# CORS 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat_router, prefix="/api/chat", tags=["对话"])
app.include_router(threads_router, prefix="/api/threads", tags=["会话管理"])
app.include_router(history_router, prefix="/api/history", tags=["历史记录"])
app.include_router(settings_router, prefix="/api/settings", tags=["全局配置"])
app.include_router(skills_router, prefix="/api/skills", tags=["Skills 管理"])

# 静态文件服务：视频产物（与 bridge 写入目录完全一致）
app.mount("/api/files", StaticFiles(directory=tasks_root()), name="video_files")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_view.web_main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # 开发模式，代码变更自动重载
    )
