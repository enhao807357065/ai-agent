"""基于能力、优先级、成本与健康状态的 Gateway 执行路由器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Literal, Protocol, cast

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
    def _route_info(name: str, candidate: CandidateTarget, attempt: int) -> GatewayRouteInfo:
        return GatewayRouteInfo(
            logical_model=name,
            provider=candidate.profile.provider,
            upstream_model=candidate.profile.model,
            attempt=attempt,
            used_fallback=attempt > 1,
        )

    def _candidates(self, logical_model: str | None, call: GatewayModelCall, *, stream: bool) -> tuple[str, list[CandidateTarget]]:
        try:
            name, _ = self._selector.resolve_policy(logical_model)
        except GatewayCapabilityUnavailable as exc:
            raise GatewayRoutingError(str(exc)) from exc

        candidates = self._selector.select(
            logical_model,
            call,
            requirements_from_call(call, stream=stream),
        )
        logger.info(
            "gateway.candidates_selected",
            logical_model=name,
            stream=stream,
            selected_target=candidates[0].profile.id,
            eligible_targets=[candidate.profile.id for candidate in candidates],
        )
        return name, candidates

    def _create_model(self, candidate: CandidateTarget) -> StreamingModel:
        try:
            return create_model_for_target(self._provider_target(candidate.profile))
        except (ValueError, KeyError) as exc:
            logger.error("gateway.configuration_error", target_id=candidate.profile.id, error_type=type(exc).__name__)
            raise GatewayConfigurationError("Gateway provider configuration is invalid") from exc

    async def complete(self, logical_model: str | None, call: GatewayModelCall) -> GatewayModelResult:
        # 在请求出网前验证 schema 自身，避免把调用方契约错误伪装成 upstream 400。
        json_schema_from_call(call)
        name, candidates = self._candidates(logical_model, call, stream=False)
        await self._limiter.acquire_request(name)
        errors: list[str] = []

        for attempt, candidate in enumerate(candidates, start=1):
            model = self._create_model(candidate)
            started = time.perf_counter()
            try:
                logger.info("gateway.route_selected", logical_model=name, target_id=candidate.profile.id, attempt=attempt)
                completion = await model.complete(**call.model_dump())
                validate_structured_output(completion.content, call)
                self._health.record_success(candidate.profile.id, (time.perf_counter() - started) * 1000)
                usage = GatewayUsage(input_tokens=completion.input_tokens, output_tokens=completion.output_tokens)
                self._limiter.report_tokens(name, usage.total_tokens)
                return GatewayModelResult(
                    route=self._route_info(name, candidate, attempt),
                    content=completion.content,
                    tool_calls=[GatewayToolCall(id=item.id, name=item.name, arguments=item.arguments) for item in completion.tool_calls],
                    finish_reason=cast(Literal["stop", "tool_calls", "length"], completion.finish_reason),
                    usage=GatewayUsage(input_tokens=completion.input_tokens, output_tokens=completion.output_tokens),
                )
            except RETRIABLE_EXCEPTIONS as exc:
                self._health.record_retriable_failure(candidate.profile.id)
                errors.append(f"{candidate.profile.id}: {type(exc).__name__}")
                logger.warning("gateway.fallback", logical_model=name, target_id=candidate.profile.id, attempt=attempt, error=str(exc))

        raise GatewayUpstreamUnavailable(f"All eligible targets for '{name}' failed: {'; '.join(errors)}")

    async def stream(self, logical_model: str | None, call: GatewayModelCall) -> AsyncIterator[GatewayStreamEvent]:
        """首个有效文本/工具输出前可换下一个能力兼容候选；之后绝不切换。"""
        json_schema_from_call(call)
        name, candidates = self._candidates(logical_model, call, stream=True)
        await self._limiter.acquire_request(name)
        errors: list[str] = []

        for attempt, candidate in enumerate(candidates, start=1):
            model = self._create_model(candidate)
            emitted_output = False
            text_parts: list[str] = []
            started = time.perf_counter()
            try:
                logger.info("gateway.route_selected", logical_model=name, target_id=candidate.profile.id, attempt=attempt)
                async for chunk in model.stream(**call.model_dump()):
                    if isinstance(chunk, TextChunk):
                        emitted_output = True
                        text_parts.append(chunk.content)
                        yield GatewayTextDelta(content=chunk.content)
                    elif isinstance(chunk, ToolCallChunk):
                        emitted_output = True
                        yield GatewayToolCallEvent(tool_call=GatewayToolCall(
                            id=chunk.id, name=chunk.name, arguments=chunk.arguments,
                        ))
                    elif isinstance(chunk, StreamDone):
                        validate_structured_output("".join(text_parts), call)
                        self._health.record_success(candidate.profile.id, (time.perf_counter() - started) * 1000)
                        usage = GatewayUsage(input_tokens=chunk.input_tokens, output_tokens=chunk.output_tokens)
                        self._limiter.report_tokens(name, usage.total_tokens)
                        yield GatewayCompletedEvent(
                            finish_reason=cast(Literal["stop", "tool_calls", "length"], chunk.finish_reason),
                            usage=GatewayUsage(input_tokens=chunk.input_tokens, output_tokens=chunk.output_tokens),
                            route=self._route_info(name, candidate, attempt),
                        )
                return
            except RETRIABLE_EXCEPTIONS as exc:
                self._health.record_retriable_failure(candidate.profile.id)
                if emitted_output:
                    logger.warning("gateway.stream_failed_after_output", logical_model=name, target_id=candidate.profile.id, error=str(exc))
                    raise
                errors.append(f"{candidate.profile.id}: {type(exc).__name__}")
                logger.warning("gateway.stream_fallback", logical_model=name, target_id=candidate.profile.id, attempt=attempt, error=str(exc))

        raise GatewayUpstreamUnavailable(f"All eligible targets for '{name}' failed before first output: {'; '.join(errors)}")


gateway_model_router = GatewayModelRouter()
