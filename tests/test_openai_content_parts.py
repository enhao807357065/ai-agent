"""OpenAI Chat Completions content-parts compatibility regression tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app


def test_chat_completions_accepts_legacy_string_content(monkeypatch):
    """Existing string content remains the canonical downstream message shape."""
    import app.api.gateway as gateway

    captured_calls = []

    class Router:
        logical_models = ["chat-default"]

        def resolve(self, model):
            return model, None

        async def complete(self, model, call):
            captured_calls.append(call)
            from app.models.gateway import GatewayModelResult, GatewayRouteInfo, GatewayUsage
            return GatewayModelResult(
                route=GatewayRouteInfo(
                    logical_model=model,
                    target_id="test/target",
                    provider="test",
                    upstream_model="test-model",
                    attempt=1,
                    used_fallback=False,
                ),
                content="ok",
                usage=GatewayUsage(),
            )

    monkeypatch.setattr(gateway, "gateway_model_router", Router())
    with TestClient(create_app()) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "legacy request"}],
        })

    assert response.status_code == 200
    assert captured_calls[0].messages == [{"role": "user", "content": "legacy request"}]


def test_chat_completions_normalizes_text_content_parts_to_string(monkeypatch):
    """OpenAI text parts are accepted and normalized before provider adapters."""
    import app.api.gateway as gateway

    captured_calls = []

    class Router:
        logical_models = ["chat-default"]

        def resolve(self, model):
            return model, None

        async def complete(self, model, call):
            captured_calls.append(call)
            from app.models.gateway import GatewayModelResult, GatewayRouteInfo, GatewayUsage
            return GatewayModelResult(
                route=GatewayRouteInfo(
                    logical_model=model,
                    target_id="test/target",
                    provider="test",
                    upstream_model="test-model",
                    attempt=1,
                    used_fallback=False,
                ),
                content="ok",
                usage=GatewayUsage(),
            )

    monkeypatch.setattr(gateway, "gateway_model_router", Router())
    with TestClient(create_app()) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "chat-default",
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "system rules"}]},
                {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]},
            ],
        })

    assert response.status_code == 200
    assert captured_calls[0].messages == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "hello\n world"},
    ]


def test_validation_log_includes_shape_not_prompt_content(caplog):
    """Validation logging explains 422 without retaining user prompt text."""
    with TestClient(create_app()) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/private.png"}}]}],
        })

    assert response.status_code == 422
    records = [json.loads(record.getMessage()) for record in caplog.records]
    event = next(item for item in records if item["event"] == "gateway.request_validation_failed")
    assert event["error_count"] == 1
    assert event["validation_errors"] == [{
        "loc": ["body", "messages", 0, "content"],
        "type": "value_error",
        "message": "Value error, Only text content parts are supported.",
        "input_type": "list",
        "input_list_length": 1,
        "part_types": ["image_url"],
    }]
    assert "private.png" not in str(event)
