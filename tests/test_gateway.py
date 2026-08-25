"""Gateway 的统一 HTTP 契约与 primary/fallback 路由测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from fastapi.testclient import TestClient

from app.main import create_app
from decimal import Decimal

from app.models.capabilities import ModelCapability, RoutingPolicy, TargetProfile
from app.models.gateway import (
    GatewayCompletedEvent,
    GatewayModelCall,
    GatewayModelResult,
    GatewayResponseToolCallEvent,
    GatewayRouteInfo,
    GatewayTextDelta,

    GatewayToolCall,
    GatewayToolCallEvent,
    GatewayUsage,
    ModelRoute,
    ProviderTarget,
)
from app.models.streaming import CompletionResult, StreamDone, StreamingModel, TextChunk, ToolCallChunk
from app.services.gateway_candidate_selector import (
    GatewayCandidateSelector,
    GatewayCapabilityUnavailable,
)
from app.services.gateway_requirement_extractor import requirements_from_call
from app.services.gateway_structured_output_validator import (
    GatewayStructuredOutputInvalid,
    GatewayStructuredOutputSchemaError,
)
from app.services.gateway_model_router import GatewayModelRouter, GatewayUpstreamUnavailable
from app.services.rate_limiter import RateLimitExceeded
from app.services.target_health_registry import TargetHealthRegistry


class FakeModel(StreamingModel):
    def __init__(self, name: str = "fake-upstream") -> None:
        self._name = name

    @property
    def model_name(self) -> str:
        return self._name

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[TextChunk | ToolCallChunk | StreamDone]:
        async def generate():
            yield TextChunk("hello")
            if tools:
                yield ToolCallChunk("call_fake", "get_weather", {"city": "北京"})
            yield StreamDone("tool_calls" if tools else "stop", input_tokens=3, output_tokens=2)
        return generate()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        calls = [ToolCallChunk("call_fake", "get_weather", {"city": "北京"})] if tools else []
        return CompletionResult("hello", calls, "tool_calls" if calls else "stop", 3, 2)


class FakeGatewayRouter:
    logical_models = ["chat-default"]

    @staticmethod
    def _route() -> GatewayRouteInfo:
        return GatewayRouteInfo(
            logical_model="chat-default", provider="fake", upstream_model="fake-upstream",
            attempt=1, used_fallback=False,
        )

    def resolve(self, logical_model: str | None):
        if logical_model != "chat-default":
            from app.services.gateway_model_router import GatewayRoutingError
            raise GatewayRoutingError("Unknown logical model")
        return logical_model, None

    async def complete(self, logical_model: str | None, call: GatewayModelCall) -> GatewayModelResult:
        result = await FakeModel().complete(**call.model_dump())
        return GatewayModelResult(
            route=self._route(), content=result.content,
            tool_calls=[GatewayToolCall(id=item.id, name=item.name, arguments=item.arguments) for item in result.tool_calls],
            finish_reason=cast(Literal["stop", "tool_calls", "length"], result.finish_reason),
            usage=GatewayUsage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
        )

    def stream(self, logical_model: str | None, call: GatewayModelCall):
        async def events():
            async for chunk in FakeModel().stream(**call.model_dump()):
                if isinstance(chunk, TextChunk):
                    yield GatewayTextDelta(content=chunk.content)
                elif isinstance(chunk, ToolCallChunk):
                    yield GatewayToolCallEvent(
                        tool_call=GatewayToolCall(id=chunk.id, name=chunk.name, arguments=chunk.arguments)
                    )
                elif isinstance(chunk, StreamDone):
                    yield GatewayCompletedEvent(
                        finish_reason=cast(Literal["stop", "tool_calls", "length"], chunk.finish_reason),
                        usage=GatewayUsage(input_tokens=chunk.input_tokens, output_tokens=chunk.output_tokens),
                        route=self._route(),
                    )
        return events()


def client_with_fake_router(monkeypatch) -> TestClient:
    import app.api.gateway as gateway

    monkeypatch.setattr(gateway, "gateway_model_router", FakeGatewayRouter())
    return TestClient(create_app())


def gateway_request(stream: bool = False, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    return body


def test_models_expose_logical_names(monkeypatch):
    with client_with_fake_router(monkeypatch) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {"object": "gateway.model_list", "data": [{"id": "chat-default"}]}


def test_all_gateway_paths_share_one_nonstream_contract(monkeypatch):
    with client_with_fake_router(monkeypatch) as client:
        responses = [client.post(path, json=gateway_request()) for path in (
            "/v1/chat/completions", "/v1/responses", "/v1/messages",
        )]
    for response in responses:
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "gateway.response"
        assert data["model"] == "chat-default"
        assert data["content"] == "hello"
        assert data["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_all_gateway_paths_share_one_stream_contract(monkeypatch):
    tool = {"name": "get_weather", "parameters": {"type": "object"}}
    with client_with_fake_router(monkeypatch) as client:
        responses = [client.post(path, json=gateway_request(stream=True, tools=[tool])) for path in (
            "/v1/chat/completions", "/v1/responses", "/v1/messages",
        )]
    for response in responses:
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [event["type"] for event in events] == [
            "gateway.text.delta", "gateway.tool_call.completed", "gateway.completed",
        ]
        assert events[0]["content"] == "hello"
        assert events[1]["tool_call"]["name"] == "get_weather"
        assert events[-1]["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_gateway_request_and_logical_model_validation(monkeypatch):
    with client_with_fake_router(monkeypatch) as client:
        empty_messages = client.post("/v1/chat/completions", json={"model": "chat-default", "messages": []})
        invalid_temperature = client.post("/v1/responses", json={
            "model": "chat-default", "messages": [{"role": "user", "content": "hi"}], "temperature": 3,
        })
        unknown_model = client.post("/v1/messages", json={
            "model": "real-vendor-model", "messages": [{"role": "user", "content": "hi"}],
        })
    assert empty_messages.status_code == 422
    assert empty_messages.json() == {
        "object": "gateway.error",
        "error": {
            "code": "invalid_request",
            "message": "Gateway request validation failed.",
            "retryable": False,
        },
    }
    assert invalid_temperature.status_code == 422
    assert invalid_temperature.json()["error"]["code"] == "invalid_request"
    assert unknown_model.status_code == 400
    assert unknown_model.json()["object"] == "gateway.error"
    assert unknown_model.json()["error"]["code"] == "invalid_model"


def test_nonstream_upstream_failure_uses_gateway_error_response(monkeypatch):
    class UnavailableRouter(FakeGatewayRouter):
        async def complete(self, logical_model: str | None, call: GatewayModelCall) -> GatewayModelResult:
            raise GatewayUpstreamUnavailable("all targets unavailable")

    import app.api.gateway as gateway
    monkeypatch.setattr(gateway, "gateway_model_router", UnavailableRouter())
    with TestClient(create_app()) as client:
        response = client.post("/v1/chat/completions", json=gateway_request())

    assert response.status_code == 503
    assert response.json() == {
        "object": "gateway.error",
        "error": {
            "code": "upstream_unavailable",
            "message": "All upstream targets are temporarily unavailable.",
            "retryable": True,
        },
    }


def test_stream_failure_emits_gateway_error_event(monkeypatch):
    class InterruptedRouter(FakeGatewayRouter):
        def stream(self, logical_model: str | None, call: GatewayModelCall):
            async def events():
                yield GatewayTextDelta(content="partial")
                raise ConnectionError("upstream connection dropped")
            return events()

    import app.api.gateway as gateway
    monkeypatch.setattr(gateway, "gateway_model_router", InterruptedRouter())
    with TestClient(create_app()) as client:
        response = client.post("/v1/chat/completions", json=gateway_request(stream=True))

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["gateway.text.delta", "gateway.error"]
    assert events[-1]["error"] == {
        "code": "upstream_stream_interrupted",
        "message": "Upstream stream was interrupted after output began.",
        "retryable": False,
    }


def selector_for(targets: dict[str, TargetProfile], policy: RoutingPolicy, health: TargetHealthRegistry | None = None):
    return GatewayCandidateSelector(
        targets,
        {"chat-default": policy},
        health or TargetHealthRegistry(failure_threshold=2, open_seconds=60),
    )


class RecordingLimiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.reported: list[tuple[str, int]] = []

    async def acquire_request(self, model: str) -> float:
        self.acquired.append(model)
        return 0.0

    def report_tokens(self, model: str, tokens: int) -> None:
        self.reported.append((model, tokens))


def test_gateway_router_applies_logical_model_rate_limit(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "talai/primary": TargetProfile(
            id="talai/primary", provider="talai", model="primary",
            capabilities={ModelCapability.CHAT}, priority=10,
        ),
    }
    limiter = RecordingLimiter()
    monkeypatch.setattr(router_module, "create_model_for_target", lambda _: FakeModel())

    result = asyncio.run(GatewayModelRouter(
        selector_for(targets, RoutingPolicy(target_ids=["talai/primary"])),
        limiter=limiter,
    ).complete("chat-default", GatewayModelCall(messages=[{"role": "user", "content": "hi"}])))

    assert result.content == "hello"
    assert limiter.acquired == ["chat-default"]
    assert limiter.reported == [("chat-default", 5)]


def test_complete_falls_back_to_next_target(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "talai/primary": TargetProfile(
            id="talai/primary", provider="talai", model="primary",
            capabilities={ModelCapability.CHAT}, priority=10,
        ),
        "deepseek/fallback": TargetProfile(
            id="deepseek/fallback", provider="deepseek", model="fallback",
            capabilities={ModelCapability.CHAT}, priority=20,
        ),
    }
    created: list[str] = []

    class FailingModel(FakeModel):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.7,
            max_tokens: int = 4096,
            response_format: dict[str, Any] | None = None,
        ) -> CompletionResult:
            raise ConnectionError("primary unavailable")

    def factory(target: ProviderTarget) -> StreamingModel:
        created.append(target.model)
        return FailingModel() if target.model == "primary" else FakeModel("fallback")

    monkeypatch.setattr(router_module, "create_model_for_target", factory)
    result = asyncio.run(GatewayModelRouter(
        selector_for(targets, RoutingPolicy(target_ids=["talai/primary", "deepseek/fallback"]))
    ).complete(
        "chat-default", GatewayModelCall(messages=[{"role": "user", "content": "hi"}])
    ))
    assert result.content == "hello"
    assert result.route.provider == "deepseek"
    assert result.route.upstream_model == "fallback"
    assert result.route.attempt == 2
    assert result.route.used_fallback is True
    assert created == ["primary", "fallback"]


def test_stream_falls_back_only_before_first_output(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "talai/primary": TargetProfile(
            id="talai/primary", provider="talai", model="primary",
            capabilities={ModelCapability.CHAT, ModelCapability.STREAMING}, priority=10,
        ),
        "deepseek/fallback": TargetProfile(
            id="deepseek/fallback", provider="deepseek", model="fallback",
            capabilities={ModelCapability.CHAT, ModelCapability.STREAMING}, priority=20,
        ),
    }

    class FailingBeforeOutput(FakeModel):
        def stream(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.7,
            max_tokens: int = 4096,
            response_format: dict[str, Any] | None = None,
        ) -> AsyncIterator[TextChunk | ToolCallChunk | StreamDone]:
            async def generate():
                raise ConnectionError("no first token")
                yield  # pragma: no cover
            return generate()

    def factory(target: ProviderTarget) -> StreamingModel:
        return FailingBeforeOutput() if target.model == "primary" else FakeModel("fallback")

    monkeypatch.setattr(router_module, "create_model_for_target", factory)

    async def collect():
        return [event async for event in GatewayModelRouter(
            selector_for(targets, RoutingPolicy(target_ids=["talai/primary", "deepseek/fallback"]))
        ).stream(
            "chat-default", GatewayModelCall(messages=[{"role": "user", "content": "hi"}])
        )]

    events = asyncio.run(collect())
    assert isinstance(events[0], GatewayTextDelta)
    assert isinstance(events[-1], GatewayCompletedEvent)
    assert events[-1].route.provider == "deepseek"
    assert events[-1].route.upstream_model == "fallback"
    assert events[-1].route.attempt == 2
    assert events[-1].route.used_fallback is True


def test_capability_matching_filters_incompatible_targets_before_factory(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "talai/plain": TargetProfile(
            id="talai/plain", provider="talai", model="plain",
            capabilities={ModelCapability.CHAT, ModelCapability.STREAMING}, priority=1,
        ),
        "deepseek/tool": TargetProfile(
            id="deepseek/tool", provider="deepseek", model="tool",
            capabilities={ModelCapability.CHAT, ModelCapability.STREAMING, ModelCapability.TOOL_CALLING}, priority=20,
        ),
    }
    created: list[str] = []

    def factory(target: ProviderTarget) -> StreamingModel:
        created.append(target.model)
        return FakeModel(target.model)

    monkeypatch.setattr(router_module, "create_model_for_target", factory)
    router = GatewayModelRouter(selector_for(targets, RoutingPolicy(target_ids=list(targets))))
    call = GatewayModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "weather", "parameters": {}}}],
    )
    result = asyncio.run(router.complete("chat-default", call))

    assert result.route.upstream_model == "tool"
    assert created == ["tool"]


def test_json_schema_without_compatible_target_is_capability_unavailable():
    targets = {
        "talai/json-object": TargetProfile(
            id="talai/json-object", provider="talai", model="json-object",
            capabilities={ModelCapability.CHAT, ModelCapability.JSON_OBJECT},
        ),
    }
    selector = selector_for(targets, RoutingPolicy(target_ids=list(targets)))
    call = GatewayModelCall(
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema", "json_schema": {"schema": {}}},
    )

    try:
        selector.select("chat-default", call, requirements_from_call(call, stream=False))
    except GatewayCapabilityUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected capability mismatch")


def test_candidate_sorting_uses_priority_then_cost_not_model_name():
    targets = {
        "talai/expensive-low-priority": TargetProfile(
            id="talai/expensive-low-priority", provider="talai", model="largest-model",
            capabilities={ModelCapability.CHAT}, priority=30,
            input_cost_per_million=Decimal("100"), output_cost_per_million=Decimal("100"),
        ),
        "deepseek/cheap-high-priority": TargetProfile(
            id="deepseek/cheap-high-priority", provider="deepseek", model="small-model",
            capabilities={ModelCapability.CHAT}, priority=10,
            input_cost_per_million=Decimal("1"), output_cost_per_million=Decimal("1"),
        ),
    }
    selector = selector_for(targets, RoutingPolicy(target_ids=list(targets)))
    call = GatewayModelCall(messages=[{"role": "user", "content": "hi"}])
    from app.services.gateway_requirement_extractor import requirements_from_call

    candidates = selector.select("chat-default", call, requirements_from_call(call, stream=False))
    assert [candidate.profile.id for candidate in candidates] == [
        "deepseek/cheap-high-priority", "talai/expensive-low-priority",
    ]


def test_open_circuit_is_skipped_by_candidate_selection():
    targets = {
        "talai/preferred": TargetProfile(
            id="talai/preferred", provider="talai", model="preferred",
            capabilities={ModelCapability.CHAT}, priority=10,
        ),
        "deepseek/backup": TargetProfile(
            id="deepseek/backup", provider="deepseek", model="backup",
            capabilities={ModelCapability.CHAT}, priority=20,
        ),
    }
    health = TargetHealthRegistry(failure_threshold=2, open_seconds=60)
    health.record_retriable_failure("talai/preferred")
    health.record_retriable_failure("talai/preferred")
    selector = selector_for(targets, RoutingPolicy(target_ids=list(targets)), health)
    call = GatewayModelCall(messages=[{"role": "user", "content": "hi"}])
    from app.services.gateway_requirement_extractor import requirements_from_call

    candidates = selector.select("chat-default", call, requirements_from_call(call, stream=False))
    assert [candidate.profile.id for candidate in candidates] == ["deepseek/backup"]


def test_json_schema_nonstream_output_is_locally_validated(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "deepseek_responses/schema": TargetProfile(
            id="deepseek_responses/schema", provider="deepseek_responses", model="schema",
            capabilities={ModelCapability.CHAT, ModelCapability.JSON_SCHEMA},
        ),
    }

    class InvalidJsonModel(FakeModel):
        async def complete(
            self, messages, tools=None, temperature=0.7, max_tokens=4096, response_format=None,
        ) -> CompletionResult:
            return CompletionResult("not json", [], "stop", 3, 2)

    monkeypatch.setattr(router_module, "create_model_for_target", lambda _target: InvalidJsonModel())
    router = GatewayModelRouter(selector_for(targets, RoutingPolicy(target_ids=list(targets))))
    call = GatewayModelCall(
        messages=[{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object", "required": ["answer"]}},
        },
    )

    try:
        asyncio.run(router.complete("chat-default", call))
    except GatewayStructuredOutputInvalid:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected local structured-output validation failure")


def test_json_schema_validation_passes_for_matching_output(monkeypatch):
    import app.services.gateway_model_router as router_module

    targets = {
        "deepseek_responses/schema": TargetProfile(
            id="deepseek_responses/schema", provider="deepseek_responses", model="schema",
            capabilities={ModelCapability.CHAT, ModelCapability.JSON_SCHEMA},
        ),
    }

    class ValidJsonModel(FakeModel):
        async def complete(
            self, messages, tools=None, temperature=0.7, max_tokens=4096, response_format=None,
        ) -> CompletionResult:
            return CompletionResult('{"answer":"hello"}', [], "stop", 3, 2)

    monkeypatch.setattr(router_module, "create_model_for_target", lambda _target: ValidJsonModel())
    router = GatewayModelRouter(selector_for(targets, RoutingPolicy(target_ids=list(targets))))
    call = GatewayModelCall(
        messages=[{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}},
            },
        },
    )
    result = asyncio.run(router.complete("chat-default", call))
    assert result.content == '{"answer":"hello"}'


def test_invalid_json_schema_is_rejected_before_adapter_factory(monkeypatch):
    import app.services.gateway_model_router as router_module

    created = False
    def factory(_target):
        nonlocal created
        created = True
        return FakeModel()

    targets = {
        "deepseek_responses/schema": TargetProfile(
            id="deepseek_responses/schema", provider="deepseek_responses", model="schema",
            capabilities={ModelCapability.CHAT, ModelCapability.JSON_SCHEMA},
        ),
    }
    monkeypatch.setattr(router_module, "create_model_for_target", factory)
    router = GatewayModelRouter(selector_for(targets, RoutingPolicy(target_ids=list(targets))))
    call = GatewayModelCall(
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema", "json_schema": {"schema": {"type": 123}}},
    )

    try:
        asyncio.run(router.complete("chat-default", call))
    except GatewayStructuredOutputSchemaError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected invalid caller schema")
    assert created is False
