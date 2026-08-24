"""
DeepSeek 原厂 API 的 StreamingModel 实现

直连 https://api.deepseek.com，支持 thinking 模式等原厂特性。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from app.models.streaming import StreamingModel, TextChunk, ToolCallChunk, StreamDone, StreamChunk


class DeepSeekStreamingModel(StreamingModel):
    """DeepSeek 原厂 API 流式模型"""

    BASE_URL = "https://api.deepseek.com"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-pro",
        enable_thinking: bool = False,
        reasoning_effort: str = "medium",
    ):
        self._model = model
        self._enable_thinking = enable_thinking
        self._reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.BASE_URL,
            timeout=120.0,  # 思考模式可能较慢
            max_retries=1,
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """调用 DeepSeek 原厂流式接口"""

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # DeepSeek 原厂特性：思考模式
        if self._enable_thinking:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": self._reasoning_effort,
            }

        stream = await self._client.chat.completions.create(**kwargs)

        # 收集工具调用片段（流式 tool_calls 分片到达）
        tool_call_buffers: dict[int, dict] = {}
        input_tokens = 0
        output_tokens = 0
        finish_reason = "stop"

        async for chunk in stream:
            if not chunk.choices:
                # usage chunk（最后一个带 usage 的空 choices）
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # 文本 delta
            if delta.content:
                yield TextChunk(content=delta.content)

            # 工具调用 delta（流式拼装）
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    buf = tool_call_buffers[idx]
                    if tc_delta.id:
                        buf["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            buf["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            buf["arguments"] += tc_delta.function.arguments

            # finish_reason
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        # 流结束后，输出完整的 tool_calls
        for _idx in sorted(tool_call_buffers.keys()):
            buf = tool_call_buffers[_idx]
            try:
                args = json.loads(buf["arguments"]) if buf["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": buf["arguments"]}
            yield ToolCallChunk(
                id=buf["id"],
                name=buf["name"],
                arguments=args,
            )

        yield StreamDone(
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
