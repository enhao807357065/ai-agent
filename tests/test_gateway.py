"""Gateway contract tests: no real database or upstream LLM required."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.streaming import CompletionResult, StreamDone, StreamingModel, TextChunk, ToolCallChunk


class FakeModel(StreamingModel):
    @property
    def model_name(self) -> str:
        return "fake-gateway-model"

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


def client_with_fake_model(monkeypatch) -> TestClient:
    import app.api.gateway as gateway

    monkeypatch.setattr(gateway, "create_model", lambda _model=None: FakeModel())
    return TestClient(create_app())


def test_models(monkeypatch):
    with client_with_fake_model(monkeypatch) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fake-gateway-model"


def test_chat_completion_non_stream(monkeypatch):
    with client_with_fake_model(monkeypatch) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "anything",
            "messages": [{"role": "user", "content": "hi"}],
        })
    data = response.json()
    assert response.status_code == 200
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["usage"]["total_tokens"] == 5


def test_chat_completion_stream_with_tool(monkeypatch):
    tool = {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
    with client_with_fake_model(monkeypatch) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "tools": [tool], "stream": True,
        })
    assert response.status_code == 200
    assert '"content": "hello"' in response.text
    assert '"name": "get_weather"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


def test_responses_non_stream(monkeypatch):
    with client_with_fake_model(monkeypatch) as client:
        response = client.post("/v1/responses", json={"model": "anything", "instructions": "be concise", "input": "hi"})
    data = response.json()
    assert response.status_code == 200
    assert data["object"] == "response"
    assert data["output_text"] == "hello"


def test_anthropic_messages_non_stream_and_stream(monkeypatch):
    payload = {"model": "anything", "max_tokens": 32, "system": "be concise", "messages": [{"role": "user", "content": "hi"}]}
    with client_with_fake_model(monkeypatch) as client:
        response = client.post("/v1/messages", json=payload)
        stream_response = client.post("/v1/messages", json={**payload, "stream": True})
    assert response.status_code == 200
    assert response.json()["content"] == [{"type": "text", "text": "hello"}]
    assert stream_response.status_code == 200
    assert "event: message_start" in stream_response.text
    assert "event: message_stop" in stream_response.text
