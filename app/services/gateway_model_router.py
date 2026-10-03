"""基于能力、优先级、成本与健康状态的 Gateway 执行路由器。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, cast

import structlog

from app.adapters import create_model_for_target
from app.core.config import settings
from app.models.capabilities import CandidateTarget, TargetProfile
from app.models.gateway import (
    GatewayCompletedEvent,
    GatewayModelCall,
    GatewayModelResult,
    GatewayRouteInfo,
    GatewayStreamEvent,
    GatewayTextDelta,
    GatewayToolCall,
    GatewayToolCallEvent,
    GatewayUsage,
    ProviderTarget,
)
from app.models.streaming import StreamDone, StreamingModel, TextChunk, ToolCallChunk
from app.services.gateway_candidate_selector import (
    GatewayCandidateSelector,
    GatewayCapabilityUnavailable,
)
from app.services.gateway_requirement_extractor import requirements_from_call
from app.services.gateway_structured_output_validator import (
    GatewayStructuredOutputInvalid,
    GatewayStructuredOutputSchemaError,
    json_schema_from_call,
    validate_structured_output,
)
from app.services.target_health_registry import TargetHealthRegistry
from app.services.rate_limiter import RateLimitExceeded, rate_limiter

logger = structlog.get_logger(__name__)

RETRIABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)

# 一个候选 target 先在自身内消化短暂故障，耗尽后才允许 fallback。
MAX_ATTEMPTS_PER_TARGET = 3
RETRY_BASE_DELAY_SECONDS = 0.25
RETRY_MAX_DELAY_SECONDS = 2.0

try:
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    RETRIABLE_EXCEPTIONS += (APITimeoutError, APIConnectionError, InternalServerError, RateLimitError)
except ImportError:
    pass

try:
    from anthropic import APIConnectionError as AnthropicConnectionError
    from anthropic import APITimeoutError as AnthropicTimeoutError
    from anthropic import InternalServerError as AnthropicInternalServerError
    from anthropic import RateLimitError as AnthropicRateLimitError

    RETRIABLE_EXCEPTIONS += (
        AnthropicTimeoutError,
        AnthropicConnectionError,
        AnthropicInternalServerError,
        AnthropicRateLimitError,
    )
except ImportError:
    pass


class GatewayRoutingError(Exception):
    """逻辑模型不存在或路由策略无效。"""


class GatewayUpstreamUnavailable(Exception):
    """全部能力兼容且健康的候选目标均无法完成请求。"""


class GatewayConfigurationError(Exception):
    """网关自身的 provider、endpoint 或凭证配置错误；禁止 fallback。"""


class GatewayUpstreamRequestError(Exception):
    """上游拒绝请求（例如 400/401/403/422）；禁止 fallback。"""


# 诊断日志允许记录上游稳定字段，但绝不能记录 prompt、tool arguments、Authorization 或原始 body。
_SECRET_FIELD_NAMES = frozenset({"api_key", "authorization", "input", "messages", "prompt", "content", "tools", "arguments"})
_SENSITIVE_TEXT = re.compile(r"(?:sk-[A-Za-z0-9_-]+|Bearer\s+\S+|api[_ -]?key\s*[:=]\s*\S+)", re.IGNORECASE)


def upstream_rejection_diagnostics(exc: BaseException) -> dict[str, Any]:
    """提取可行动但脱敏的上游 4xx 诊断，供服务端日志排查协议不兼容。"""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        try:
            candidate = response.json() if response is not None else None
        except Exception:  # 上游 body 不可解析时，宁可无诊断也不读取原始文本。
            candidate = None
        body = candidate if isinstance(candidate, dict) else {}

    error = body.get("error", body)
    error = error if isinstance(error, dict) else {}
    fields = {
        "upstream_status_code": getattr(exc, "status_code", None),
        "upstream_error_type": error.get("type") or error.get("code"),
        "upstream_error_code": error.get("code"),
        "upstream_error_param": error.get("param"),
        "upstream_error_message": error.get("message"),
    }
    safe: dict[str, Any] = {}
    for name, value in fields.items():
        if value is None or name in _SECRET_FIELD_NAMES:
            continue
        if isinstance(value, (str, int, float, bool)):
            rendered = str(value)
            if not _SENSITIVE_TEXT.search(rendered):
                safe[name] = rendered[:500]
    return safe


class GatewayRateLimiter(Protocol):
    async def acquire_request(self, model_key: str) -> float: ...

    def report_tokens(self, model_key: str, tokens: int) -> None: ...


class GatewayModelRouter:
    """仅在能力兼容候选中，按 priority/cost/health 执行受限故障转移。"""

    def __init__(
        self,
        selector: GatewayCandidateSelector | None = None,
        limiter: GatewayRateLimiter = rate_limiter,
    ) -> None:
        self._limiter = limiter
        self._health = TargetHealthRegistry(
            failure_threshold=settings.GATEWAY_CIRCUIT_FAILURE_THRESHOLD,
            open_seconds=settings.GATEWAY_CIRCUIT_OPEN_SECONDS,
        )
        self._selector = selector or GatewayCandidateSelector(
            settings.gateway_target_registry,
            settings.gateway_routing_policies,
            self._health,
        )

    @property
    def logical_models(self) -> list[str]:
        return self._selector.logical_models

    def resolve(self, logical_model: str | None):
        try:
            return self._selector.resolve_policy(logical_model)
        except GatewayCapabilityUnavailable as exc:
            raise GatewayRoutingError(str(exc)) from exc

    @staticmethod
    def _provider_target(profile: TargetProfile) -> ProviderTarget:
        return ProviderTarget(
            provider=profile.provider,
            model=profile.model,
            enabled=profile.enabled,
            enable_thinking=profile.enable_thinking,
            thinking_budget_tokens=profile.thinking_budget_tokens,
            reasoning_effort=profile.reasoning_effort,
        )

    @staticmethod
    def _retry_delay(retry_index: int) -> float:
        """返回当前 target 的下一次重试等待时间（retry_index 从 0 开始）。"""
        return min(RETRY_BASE_DELAY_SECONDS * 2 ** retry_index, RETRY_MAX_DELAY_SECONDS)

    @staticmethod
    def _route_info(name: str, candidate: CandidateTarget, attempt: int) -> GatewayRouteInfo:
        return GatewayRouteInfo(
            logical_model=name,
            target_id=candidate.profile.id,
            provider=candidate.profile.provider,
            upstream_model=candidate.profile.model,
            attempt=attempt,
            used_fallback=attempt > 1,
        )

    def _candidates(self, logical_model: str | None, call: GatewayModelCall, *, stream: bool) -> tuple[str, list[CandidateTarget]]:
        try:
            name, policy = self._selector.resolve_policy(logical_model)
        except GatewayCapabilityUnavailable as exc:
            raise GatewayRoutingError(str(exc)) from exc

        requirements = requirements_from_call(call, stream=stream)
        candidates, rejected_targets = self._selector.select_with_diagnostics(
            logical_model,
            call,
            requirements,
        )
        logger.info(
            "gateway.candidates_selected",
            logical_model=name,
            stream=stream,
            required_capabilities=sorted(capability.value for capability in requirements.capabilities | policy.required_capabilities),
            selection_order=policy.selection_order,
            selected_target=candidates[0].profile.id,
            eligible_targets=[candidate.profile.id for candidate in candidates],
            candidates=[
                {
                    "rank": rank,
                    "target_id": candidate.profile.id,
                    "provider": candidate.profile.provider,
                    "upstream_model": candidate.profile.model,
                    "priority": candidate.profile.priority,
                    "estimated_cost": str(candidate.estimated_cost),
                    "health_score": candidate.health_score,
                }
                for rank, candidate in enumerate(candidates, start=1)
            ],
            rejected_targets=rejected_targets,
        )
        return name, candidates

    def _create_model(self, candidate: CandidateTarget) -> StreamingModel:
        try:
            return create_model_for_target(self._provider_target(candidate.profile))
        except (ValueError, KeyError) as exc:
            logger.error("gateway.configuration_error", target_id=candidate.profile.id, error_type=type(exc).__name__)
            raise GatewayConfigurationError("Gateway provider configuration is invalid") from exc

    async def complete(self, logical_model: str | None, call: GatewayModelCall) -> GatewayModelResult:
        """执行非流式请求：每个 target 最多尝试三次，随后才按候选顺序 fallback。"""
        request_started = time.perf_counter()
        json_schema_from_call(call)
        name, candidates = self._candidates(logical_model, call, stream=False)
        await self._limiter.acquire_request(name)
        errors: list[str] = []
        total_attempts = 0

        for candidate_index, candidate in enumerate(candidates, start=1):
            model = self._create_model(candidate)
            for target_attempt in range(1, MAX_ATTEMPTS_PER_TARGET + 1):
                total_attempts += 1
                attempt_started = time.perf_counter()
                try:
                    logger.info(
                        "gateway.route_selected",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                    )
                    completion = await model.complete(**call.model_dump(exclude_none=True))
                    validate_structured_output(completion.content, call)
                    attempt_latency_ms = (time.perf_counter() - attempt_started) * 1000
                    self._health.record_success(candidate.profile.id, attempt_latency_ms)
                    usage = GatewayUsage(input_tokens=completion.input_tokens, output_tokens=completion.output_tokens)
                    self._limiter.report_tokens(name, usage.total_tokens)
                    result = GatewayModelResult(
                        route=self._route_info(name, candidate, candidate_index),
                        content=completion.content,
                        tool_calls=[GatewayToolCall(id=item.id, name=item.name, arguments=item.arguments) for item in completion.tool_calls],
                        finish_reason=cast(Literal["stop", "tool_calls", "length"], completion.finish_reason),
                        usage=usage,
                    )
                    logger.info(
                        "gateway.request_completed",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        provider=candidate.profile.provider,
                        upstream_model=candidate.profile.model,
                        stream=False,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        total_latency_ms=round((time.perf_counter() - request_started) * 1000),
                    )
                    return result
                except RETRIABLE_EXCEPTIONS as exc:
                    self._health.record_retriable_failure(candidate.profile.id)
                    errors.append(f"{candidate.profile.id}#{target_attempt}: {type(exc).__name__}")
                    if target_attempt == MAX_ATTEMPTS_PER_TARGET:
                        logger.warning(
                            "gateway.fallback",
                            logical_model=name,
                            target_id=candidate.profile.id,
                            candidate_index=candidate_index,
                            target_attempt=target_attempt,
                            total_attempts=total_attempts,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        break
                    delay = self._retry_delay(target_attempt - 1)
                    logger.warning(
                        "gateway.retry_scheduled",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                        retry_delay_ms=round(delay * 1000),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code not in {400, 401, 403, 422}:
                        raise
                    # 上游明确拒绝时不 retry/fallback；只记录脱敏协议诊断，公开响应仍由 API 层统一归一化。
                    logger.warning(
                        "gateway.upstream_request_rejected",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        provider=candidate.profile.provider,
                        upstream_model=candidate.profile.model,
                        stream=False,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                        **upstream_rejection_diagnostics(exc),
                    )
                    raise

        logger.error(
            "gateway.request_failed",
            logical_model=name,
            stream=False,
            total_attempts=total_attempts,
            total_latency_ms=round((time.perf_counter() - request_started) * 1000),
            error_type="GatewayUpstreamUnavailable",
            errors=errors,
        )
        raise GatewayUpstreamUnavailable(f"All eligible targets for '{name}' failed: {'; '.join(errors)}")

    async def stream(self, logical_model: str | None, call: GatewayModelCall) -> AsyncIterator[GatewayStreamEvent]:
        """首个有效输出前可重试或 fallback；首个输出后绝不切换 target。"""
        request_started = time.perf_counter()
        json_schema_from_call(call)
        name, candidates = self._candidates(logical_model, call, stream=True)
        await self._limiter.acquire_request(name)
        errors: list[str] = []
        total_attempts = 0

        for candidate_index, candidate in enumerate(candidates, start=1):
            model = self._create_model(candidate)
            for target_attempt in range(1, MAX_ATTEMPTS_PER_TARGET + 1):
                total_attempts += 1
                emitted_output = False
                ttft_ms: int | None = None
                text_parts: list[str] = []
                attempt_started = time.perf_counter()
                try:
                    logger.info(
                        "gateway.route_selected",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                    )
                    async for chunk in model.stream(**call.model_dump(exclude_none=True)):
                        if isinstance(chunk, TextChunk):
                            if not emitted_output:
                                ttft_ms = round((time.perf_counter() - request_started) * 1000)
                            emitted_output = True
                            text_parts.append(chunk.content)
                            yield GatewayTextDelta(content=chunk.content)
                        elif isinstance(chunk, ToolCallChunk):
                            if not emitted_output:
                                ttft_ms = round((time.perf_counter() - request_started) * 1000)
                            emitted_output = True
                            yield GatewayToolCallEvent(tool_call=GatewayToolCall(
                                id=chunk.id, name=chunk.name, arguments=chunk.arguments,
                            ))
                        elif isinstance(chunk, StreamDone):
                            validate_structured_output("".join(text_parts), call)
                            attempt_latency_ms = (time.perf_counter() - attempt_started) * 1000
                            self._health.record_success(candidate.profile.id, attempt_latency_ms)
                            usage = GatewayUsage(input_tokens=chunk.input_tokens, output_tokens=chunk.output_tokens)
                            self._limiter.report_tokens(name, usage.total_tokens)
                            logger.info(
                                "gateway.request_completed",
                                logical_model=name,
                                target_id=candidate.profile.id,
                                provider=candidate.profile.provider,
                                upstream_model=candidate.profile.model,
                                stream=True,
                                candidate_index=candidate_index,
                                target_attempt=target_attempt,
                                total_attempts=total_attempts,
                                input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens,
                                total_tokens=usage.total_tokens,
                                total_latency_ms=round((time.perf_counter() - request_started) * 1000),
                                ttft_ms=ttft_ms,
                            )
                            yield GatewayCompletedEvent(
                                finish_reason=cast(Literal["stop", "tool_calls", "length"], chunk.finish_reason),
                                usage=usage,
                                route=self._route_info(name, candidate, candidate_index),
                            )
                    return
                except RETRIABLE_EXCEPTIONS as exc:
                    self._health.record_retriable_failure(candidate.profile.id)
                    if emitted_output:
                        logger.error(
                            "gateway.request_failed",
                            logical_model=name,
                            target_id=candidate.profile.id,
                            provider=candidate.profile.provider,
                            upstream_model=candidate.profile.model,
                            stream=True,
                            candidate_index=candidate_index,
                            target_attempt=target_attempt,
                            total_attempts=total_attempts,
                            total_latency_ms=round((time.perf_counter() - request_started) * 1000),
                            ttft_ms=ttft_ms,
                            error_type=type(exc).__name__,
                            error=str(exc),
                            emitted_output=True,
                        )
                        raise

                    errors.append(f"{candidate.profile.id}#{target_attempt}: {type(exc).__name__}")
                    if target_attempt == MAX_ATTEMPTS_PER_TARGET:
                        logger.warning(
                            "gateway.stream_fallback",
                            logical_model=name,
                            target_id=candidate.profile.id,
                            candidate_index=candidate_index,
                            target_attempt=target_attempt,
                            total_attempts=total_attempts,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        break
                    delay = self._retry_delay(target_attempt - 1)
                    logger.warning(
                        "gateway.stream_retry_scheduled",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                        retry_delay_ms=round(delay * 1000),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code not in {400, 401, 403, 422}:
                        raise
                    # 流尚未输出时也记录同一套脱敏字段；一旦已输出则客户端仍按中断语义处理。
                    logger.warning(
                        "gateway.upstream_request_rejected",
                        logical_model=name,
                        target_id=candidate.profile.id,
                        provider=candidate.profile.provider,
                        upstream_model=candidate.profile.model,
                        stream=True,
                        candidate_index=candidate_index,
                        target_attempt=target_attempt,
                        total_attempts=total_attempts,
                        emitted_output=emitted_output,
                        **upstream_rejection_diagnostics(exc),
                    )
                    raise

        logger.error(
            "gateway.request_failed",
            logical_model=name,
            stream=True,
            total_attempts=total_attempts,
            total_latency_ms=round((time.perf_counter() - request_started) * 1000),
            error_type="GatewayUpstreamUnavailable",
            errors=errors,
            emitted_output=False,
        )
        raise GatewayUpstreamUnavailable(f"All eligible targets for '{name}' failed before first output: {'; '.join(errors)}")


gateway_model_router = GatewayModelRouter()
