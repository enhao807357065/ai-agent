"""
FastAPI 应用入口

启动方式：
    cd ~/work/py/ai-agent
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.gateway import router as gateway_router
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

    @app.exception_handler(RequestValidationError)
    async def gateway_validation_error_handler(request: Request, exc: RequestValidationError):
        """Gateway 的请求校验也使用统一错误 envelope；其他 API 保持 FastAPI 默认格式。"""
        if request.url.path in {"/v1/chat/completions", "/v1/responses", "/v1/messages"}:
            return JSONResponse(
                status_code=422,
                content={
                    "object": "gateway.error",
                    "error": {
                        "code": "invalid_request",
                        "message": "Gateway request validation failed.",
                        "retryable": False,
                    },
                },
            )
        return await request_validation_exception_handler(request, exc)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        """为每个 HTTP 请求绑定 trace_id，使日志可按请求链路关联。"""
        trace_id = (
            request.headers.get("X-Trace-Id")
            or request.headers.get("X-Request-Id")
            or uuid.uuid4().hex
        )
        started_at = time.perf_counter()
        logger = structlog.get_logger(__name__)

        # request.state 供路由或下游 HTTP 调用读取并透传。
        request.state.trace_id = trace_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

        logger.info(
            "http.request_received",
            method=request.method,
            path=request.url.path,
            query=str(request.url.query) or None,
            client_host=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - started_at) * 1000)
            response.headers["X-Trace-Id"] = trace_id
            logger.info(
                "http.request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000)
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()

    # 注册路由
    app.include_router(router)
    # OpenAI Chat / Responses / Anthropic Messages 兼容网关
    app.include_router(gateway_router)

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
            "base_url": settings.TALAI_BASE_URL,
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
            enabled=settings.RATE_LIMIT_ENABLED,
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
