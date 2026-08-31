"""Adapter 与供应商 SDK 的离线契约测试。

这些测试不请求网络。它们以接近真实 OpenAI / Anthropic SDK 的请求入口和
响应对象形状验证 Adapter 的转换边界，避免 Router 的 FakeModel 掩盖 SDK
参数名或响应字段变化。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app.adapters.deepseek_adapter import DeepSeekAnthropicModel
from app.adapters.deepseek_responses_adapter import DeepSeekResponsesModel
from app.adapters.talai_adapter import TalAIStreamingModel


class RecordingAsyncMethod:
    """模拟 SDK 的 async create(**kwargs)，同时保存精确请求参数。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


def test_openai_compatible_complete_preserves_sdk_kwargs_and_normalizes_response():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="天气晴朗",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_weather",
                            function=SimpleNamespace(
                                name="get_weather",
                                arguments='{"city":"北京"}',
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )
    create = RecordingAsyncMethod(response)
    model = TalAIStreamingModel(api_key="test", base_url="https://example.test/v1", model="tal-model")
    model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = asyncio.run(model.complete(
        messages=[{"role": "user", "content": "北京天气"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
        temperature=0.2,
        max_tokens=128,
        response_format={"type": "json_object"},
    ))

    assert create.calls == [{
        "model": "tal-model",
        "messages": [{"role": "user", "content": "北京天气"}],
        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 128,
        "stream": False,
        "response_format": {"type": "json_object"},
    }]
    assert result.content == "天气晴朗"
    assert result.finish_reason == "tool_calls"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_weather", "get_weather", {"city": "北京"})
    ]


def test_anthropic_complete_extracts_system_converts_tools_and_normalizes_response():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="我来处理。"),
            SimpleNamespace(type="tool_use", id="tool_1", name="lookup_order", input={"order_id": "A-1"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=13, output_tokens=9),
    )
    create = RecordingAsyncMethod(response)
    model = DeepSeekAnthropicModel(api_key="test", model="deepseek-chat")
    model._client = SimpleNamespace(messages=SimpleNamespace(create=create))

    result = asyncio.run(model.complete(
        messages=[
            {"role": "system", "content": "你是客服助手"},
            {"role": "user", "content": "查订单 A-1"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "lookup_order",
                "description": "查询订单",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            },
        }],
        temperature=0.7,
        max_tokens=256,
    ))

    assert create.calls == [{
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "查订单 A-1"}],
        "max_tokens": 256,
        "extra_body": {"temperature": 0.7},
        "system": "你是客服助手",
        "tools": [{
            "name": "lookup_order",
            "description": "查询订单",
            "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        }],
        "tool_choice": {"type": "auto"},
    }]
    assert result.content == "我来处理。"
    assert result.finish_reason == "tool_calls"
    assert (result.input_tokens, result.output_tokens) == (13, 9)
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("tool_1", "lookup_order", {"order_id": "A-1"})
    ]


def test_responses_complete_uses_responses_contract_and_normalizes_function_call():
    response = SimpleNamespace(
        output_text="订单状态：已发货",
        output=[
            SimpleNamespace(
                type="function_call",
                id="fc_item_1",
                call_id="call_order",
                name="lookup_order",
                arguments='{"order_id":"A-1"}',
            )
        ],
        status="completed",
        usage=SimpleNamespace(input_tokens=17, output_tokens=8),
    )
    create = RecordingAsyncMethod(response)
    model = DeepSeekResponsesModel(api_key="test", model="deepseek-v4-flash", reasoning_effort="low")
    model._client = SimpleNamespace(responses=SimpleNamespace(create=create))

    result = asyncio.run(model.complete(
        messages=[
            {"role": "system", "content": "你是订单助手"},
            {"role": "user", "content": "查订单 A-1"},
        ],
        tools=[{"type": "function", "function": {"name": "lookup_order", "parameters": {"type": "object"}}}],
        temperature=0.3,
        max_tokens=512,
        response_format={"type": "json_object"},
    ))

    assert create.calls == [{
        "model": "deepseek-v4-flash",
        "input": "查订单 A-1",
        "stream": False,
        "max_output_tokens": 512,
        "temperature": 0.3,
        "instructions": "你是订单助手",
        "tools": [{"type": "function", "name": "lookup_order", "description": "", "parameters": {"type": "object"}}],
        "reasoning": {"effort": "low"},
        "text": {"format": {"type": "json_object"}},
    }]
    assert result.content == "订单状态：已发货"
    assert result.finish_reason == "tool_calls"
    assert (result.input_tokens, result.output_tokens) == (17, 8)
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_order", "lookup_order", {"order_id": "A-1"})
    ]
