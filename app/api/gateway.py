"""统一 LLM Gateway HTTP API。

三个历史 URL 仅作为相同 Gateway 契约的路径别名：它们使用同一组 Pydantic
入参、出参与 SSE 事件，彻底屏蔽 TAL / DeepSeek 等底层厂商协议差异。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import structlog
import asyncio
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.models.gateway import (
    GatewayCompletedEvent,
    GatewayError,
    GatewayErrorResponse,
    GatewayModelCall,
    GatewayModelInfo,
    GatewayModelsResponse,
    GatewayRequest,
    GatewayResponse,
    GatewayResponseCompletedEvent,
    GatewayResponseErrorEvent,
    GatewayResponseTextDelta,
    GatewayResponseToolCallEvent,
    GatewayTextDelta,
    GatewayToolCallEvent,
)
from app.services.gateway_candidate_selector import GatewayCapabilityUnavailable
from app.services.gateway_structured_output_validator import (
    GatewayStructuredOutputInvalid,
    GatewayStructuredOutputSchemaError,
    json_schema_from_call,
)
from app.services.rate_limiter import RateLimitExceeded
from app.services.gateway_model_router import (
    GatewayConfigurationError,
    GatewayRoutingError,
    GatewayUpstreamUnavailable,
    gateway_model_router,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["LLM Gateway"])


def _id(prefix: str = "gwresp") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> int:
    return int(time.time())


def _sse(event: BaseModel) -> str:
    """统一 Gateway SSE：event type 位于 JSON 的 ``type`` 字段。"""
    return f"data: {event.model_dump_json()}\n\n"


def _gateway_error(
    exc: Exception,
    *,
    stream_started: bool = False,
) -> tuple[int, GatewayError]:
    """将领域/SDK 异常转换为稳定的公开错误码，绝不返回原始 provider 信息。"""
    if isinstance(exc, RateLimitExceeded):
        return 429, GatewayError(
            code="rate_limited",
            message="Gateway rate limit exceeded; retry later.",
            retryable=True,
        )
    if isinstance(exc, GatewayRoutingError):
        return 400, GatewayError(code="invalid_model", message=str(exc))
    if isinstance(exc, GatewayCapabilityUnavailable):
        return (503 if exc.retryable else 422), GatewayError(
            code="upstream_unavailable" if exc.retryable else "capability_unavailable",
            message=(
                "All capability-compatible upstream targets are temporarily unavailable."
                if exc.retryable
                else "No target satisfies the requested capabilities."
            ),
            retryable=exc.retryable,
        )
    if isinstance(exc, GatewayStructuredOutputSchemaError):
        return 422, GatewayError(
            code="structured_output_schema_invalid",
            message="The requested JSON Schema is invalid.",
        )
    if isinstance(exc, GatewayStructuredOutputInvalid):
        return 502, GatewayError(
            code="structured_output_invalid",
            message="Upstream output did not satisfy the requested JSON Schema.",
        )
    if isinstance(exc, GatewayConfigurationError):
        return 500, GatewayError(
            code="gateway_configuration_error",
            message="Gateway provider configuration is invalid.",
        )
    if isinstance(exc, GatewayUpstreamUnavailable):
        return 503, GatewayError(
            code="upstream_unavailable",
            message="All upstream targets are temporarily unavailable.",
            retryable=True,
        )
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return 502, GatewayError(
            code="upstream_stream_interrupted" if stream_started else "upstream_unavailable",
            message=(
                "Upstream stream was interrupted after output began."
                if stream_started
                else "Upstream service is temporarily unavailable."
            ),
            retryable=not stream_started,
        )

    # 适配不同 SDK：OpenAI / Anthropic 的 HTTP 异常都公开 status_code。
    status_code = getattr(exc, "status_code", None)
    if status_code in {400, 401, 403, 422}:
        return int(status_code), GatewayError(
            code="upstream_request_error",
            message="Upstream rejected the request.",
        )

    logger.exception("gateway.unhandled_error", error_type=type(exc).__name__)
    return 500, GatewayError(code="internal_error", message="Gateway internal error.")


def _error_response(exc: Exception) -> JSONResponse:
    status_code, error = _gateway_error(exc)
    return JSONResponse(
        status_code=status_code,
        content=GatewayErrorResponse(error=error).model_dump(),
    )


def _to_model_call(request: GatewayRequest) -> GatewayModelCall:
    """公共 HTTP 请求 → Router 的内部调用 Command。"""
    return GatewayModelCall(
        messages=[message.model_dump(exclude_none=True) for message in request.messages],
        tools=[
            {
                "type": "function",
                "function": tool.model_dump(),
            }
            for tool in request.tools
        ] if request.tools else None,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        response_format=request.response_format,
    )


@router.get("/v1/models", response_model=GatewayModelsResponse)
async def list_models() -> GatewayModelsResponse:
    """列出网关对外暴露的逻辑模型名。"""
    return GatewayModelsResponse(
        data=[GatewayModelInfo(id=model) for model in gateway_model_router.logical_models]
    )


async def _gateway_response(request: GatewayRequest) -> GatewayResponse | JSONResponse:
    """三个 URL 共用的非流式执行入口，统一转换所有运行期异常。"""
    try:
        result = await gateway_model_router.complete(request.model, _to_model_call(request))
    except Exception as exc:
        return _error_response(exc)

    return GatewayResponse(
        id=_id(),
        created=_now(),
        model=request.model,
        content=result.content,
        tool_calls=result.tool_calls,
        finish_reason=result.finish_reason,
        usage=result.usage,
    )


async def _stream_gateway_response(request: GatewayRequest) -> AsyncIterator[str]:
    """三个 URL 共用的流式入口；运行期失败以 ``gateway.error`` SSE 表达。"""
    response_id = _id()
    emitted_output = False
    try:
        async for event in gateway_model_router.stream(request.model, _to_model_call(request)):
            if isinstance(event, GatewayTextDelta):
                emitted_output = True
                yield _sse(GatewayResponseTextDelta(
                    id=response_id,
                    model=request.model,
                    content=event.content,
                ))
            elif isinstance(event, GatewayToolCallEvent):
                emitted_output = True
                yield _sse(GatewayResponseToolCallEvent(
                    id=response_id,
                    model=request.model,
                    tool_call=event.tool_call,
                ))
            elif isinstance(event, GatewayCompletedEvent):
                yield _sse(GatewayResponseCompletedEvent(
                    id=response_id,
                    model=request.model,
                    finish_reason=event.finish_reason,
                    usage=event.usage,
                ))
    except Exception as exc:
        _, error = _gateway_error(exc, stream_started=emitted_output)
        logger.warning(
            "gateway.stream_error_event",
            logical_model=request.model,
            code=error.code,
            emitted_output=emitted_output,
        )
        yield _sse(GatewayResponseErrorEvent(
            id=response_id,
            model=request.model,
            error=error,
        ))


async def _handle_gateway(request: GatewayRequest) -> GatewayResponse | JSONResponse | StreamingResponse:
    logger.info("gateway.request", logical_model=request.model, stream=request.stream)

    # StreamingResponse 开始发送后无法改写为 HTTP 4xx，因此预校验必须在这里完成。
    try:
        gateway_model_router.resolve(request.model)
        json_schema_from_call(_to_model_call(request))
    except Exception as exc:
        return _error_response(exc)

    if request.stream:
        return StreamingResponse(
            _stream_gateway_response(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _gateway_response(request)


# 三条路径保持可访问，但它们不再代表三个外部厂商协议。
@router.post("/v1/chat/completions", response_model=GatewayResponse)
async def chat_completions(request: GatewayRequest) -> GatewayResponse | JSONResponse | StreamingResponse:
    return await _handle_gateway(request)


@router.post("/v1/responses", response_model=GatewayResponse)
async def responses(request: GatewayRequest) -> GatewayResponse | JSONResponse | StreamingResponse:
    return await _handle_gateway(request)


@router.post("/v1/messages", response_model=GatewayResponse)
async def anthropic_messages(request: GatewayRequest) -> GatewayResponse | JSONResponse | StreamingResponse:
    return await _handle_gateway(request)
