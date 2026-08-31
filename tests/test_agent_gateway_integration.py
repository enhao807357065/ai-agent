"""Agent Loop 对 GatewayModelRouter 进程内调用的契约测试。"""

import asyncio
from collections.abc import AsyncIterator

from app.models.gateway import (
    GatewayCompletedEvent,
    GatewayModelCall,
    GatewayModelResult,
    GatewayRouteInfo,
    GatewayTextDelta,
    GatewayUsage,
)
from app.services.agent_loop import _call_model_via_gateway
from app.api import routes
from app.services.run_store import RunState


class FakeGatewayRouter:
    def __init__(self) -> None:
        self.complete_calls: list[tuple[str | None, GatewayModelCall]] = []
        self.stream_calls: list[tuple[str | None, GatewayModelCall]] = []

    async def complete(self, logical_model: str | None, call: GatewayModelCall) -> GatewayModelResult:
        self.complete_calls.append((logical_model, call))
        return GatewayModelResult(
            route=GatewayRouteInfo(
                logical_model=logical_model or "", target_id="test/hidden", provider="test", upstream_model="hidden",
                attempt=1, used_fallback=False,
            ),
            content='{"answer":"ok"}',
            usage=GatewayUsage(input_tokens=7, output_tokens=3),
        )

    async def stream(self, logical_model: str | None, call: GatewayModelCall) -> AsyncIterator[GatewayTextDelta | GatewayCompletedEvent]:
        self.stream_calls.append((logical_model, call))
        yield GatewayTextDelta(content="hello")
        yield GatewayCompletedEvent(
            finish_reason="stop",
            usage=GatewayUsage(input_tokens=5, output_tokens=2),
            route=GatewayRouteInfo(
                logical_model=logical_model or "", target_id="test/hidden", provider="test", upstream_model="hidden",
                attempt=1, used_fallback=False,
            ),
        )


def test_agent_turn_uses_gateway_router_nonstream_without_http():
    router = FakeGatewayRouter()
    state = RunState("run-1", "chat-default")

    text, tools, reason, input_tokens, output_tokens, ttft = asyncio.run(
        _call_model_via_gateway(
            gateway_router=router,
            logical_model="chat-default",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            temperature=0.2,
            max_tokens=123,
            response_format={"type": "json_object"},
            run_state=state,
            turn=1,
            stream=False,
        )
    )

    assert text == ['{"answer":"ok"}']
    assert tools == []
    assert (reason, input_tokens, output_tokens, ttft) == ("stop", 7, 3, None)
    assert len(router.complete_calls) == 1
    logical_model, call = router.complete_calls[0]
    assert logical_model == "chat-default"
    assert call.max_tokens == 123
    assert call.response_format == {"type": "json_object"}
    assert not router.stream_calls


def test_agent_turn_converts_gateway_stream_events_to_run_events():
    router = FakeGatewayRouter()
    state = RunState("run-2", "chat-default")

    text, tools, reason, input_tokens, output_tokens, ttft = asyncio.run(
        _call_model_via_gateway(
            gateway_router=router,
            logical_model="chat-default",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            temperature=0.7,
            max_tokens=88,
            response_format=None,
            run_state=state,
            turn=1,
            stream=True,
        )
    )

    assert text == ["hello"]
    assert tools == []
    assert (reason, input_tokens, output_tokens) == ("stop", 5, 2)
    assert ttft is not None
    assert len(router.stream_calls) == 1
    assert state.events[-1].data == {"content": "hello", "turn": 1}


def test_run_rejects_json_schema_when_tools_are_present():
    request = routes.CreateRunRequest(
        model="chat-default",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{
            "name": "lookup",
            "description": "lookup a value",
            "parameters": {"type": "object"},
        }],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {"type": "object"},
            },
        },
    )

    try:
        asyncio.run(routes.create_run(request))
    except routes.HTTPException as exc:
        assert exc.status_code == 422
        assert "cannot be combined" in exc.detail
    else:
        raise AssertionError("expected json_schema + tools to be rejected")
