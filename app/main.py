"""
FastAPI 应用入口

启动方式：
    cd ~/work/py/ai-agent
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.routes import router
from app.services.run_store import run_store


def create_app() -> FastAPI:
    """应用工厂"""

    # 初始化日志
    setup_logging(level="DEBUG" if settings.DEBUG else "INFO", is_test=settings.DEBUG)

    app = FastAPI(
        title="Agent Service",
        description="基于 Agent Loop 的 LLM 服务，通过 SSE 流式输出 RunEvent",
        version="0.1.0",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(router)

    # 静态文件（前端页面）
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")

    # 根路径重定向到前端页面
    from fastapi.responses import RedirectResponse

    @app.get("/")
    async def root():
        return RedirectResponse(url="/static/index.html")

    # 健康检查
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "model": settings.LLM_MODEL,
            "base_url": settings.LLM_BASE_URL,
        }

    # 数据库初始化
    @app.on_event("startup")
    async def _init_database():
        from app.core.database import init_db
        await init_db()
        import structlog
        structlog.get_logger().info("database.initialized")

    # 限流器初始化
    @app.on_event("startup")
    async def _init_rate_limiter():
        import json as _json
        import structlog
        from app.services.rate_limiter import rate_limiter, ModelRateLimit
        rate_limits = _json.loads(settings.MODEL_RATE_LIMITS)
        for model_key, cfg in rate_limits.items():
            rate_limiter.configure(model_key, ModelRateLimit(**cfg))
        structlog.get_logger().info(
            "rate_limiter.initialized",
            models=list(rate_limits.keys()),
            configs={k: {"rpm": v.get("rpm", 60), "tpm": v.get("tpm", 100000), "max_wait": v.get("max_wait", 60.0)} for k, v in rate_limits.items()},
        )

    # 后台清理任务
    @app.on_event("startup")
    async def _start_cleanup():
        async def _cleanup_loop():
            while True:
                await asyncio.sleep(60)
                count = run_store.cleanup_expired()
                if count > 0:
                    import structlog
                    structlog.get_logger().info("run_store.cleanup", removed=count)
        asyncio.create_task(_cleanup_loop())

    # 关闭数据库连接
    @app.on_event("shutdown")
    async def _close_database():
        from app.core.database import close_db
        await close_db()
        import structlog
        structlog.get_logger().info("database.closed")

    return app


app = create_app()
