"""
DeepSeek Anthropic API 格式的 StreamingModel 实现

使用 Anthropic SDK 直连 https://api.deepseek.com/anthropic
参考文档：https://api-docs.deepseek.com/zh-cn/guides/anthropic_api

支持特性：
    - 流式输出（stream=True）
    - 结构化输出（response_format → Anthropic prefill JSON 技巧）
    - Tool Calls（Anthropic 原生格式）
    - Thinking 模式（通过 thinking 参数启用）
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.models.streaming import (
    StreamingModel, TextChunk, ToolCallChunk, StreamDone, StreamChunk, CompletionResult,
)


class DeepSeekAnthropicModel(StreamingModel):
    """DeepSeek Anthropic API 格式流式模型"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/anthropic",
        model: str = "deepseek-reasoner",
        enable_thinking: bool = False,
        thinking_budget_tokens: int = 10000,
    ):
        self._model = model
        self._enable_thinking = enable_thinking
        self._thinking_budget_tokens = thinking_budget_tokens
        self._client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
            max_retries=1,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将 OpenAI 格式的 tools 转换为 Anthropic 格式

        OpenAI: [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        Anthropic: [{"name": ..., "description": ..., "input_schema": ...}]
        """
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", tool)
            anthropic_tools.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """
        将 OpenAI 格式的 messages 转换为 Anthropic 格式

        返回 (system_prompt, anthropic_messages)

        OpenAI:
            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]
        Anthropic:
            system 单独提取，messages 里只有 user/assistant/tool_result
        """
        system_prompt = None
        anthropic_messages: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]

            if role == "system":
                # 合并多个 system 消息
                if system_prompt is None:
                    system_prompt = msg["content"]
                else:
                    system_prompt += "\n\n" + msg["content"]

            elif role == "user":
                content = msg["content"]
                if isinstance(content, str):
                    anthropic_messages.append({"role": "user", "content": content})
                else:
                    # 已经是 Anthropic content block 格式
                    anthropic_messages.append({"role": "user", "content": content})

            elif role == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")

                if tool_calls:
                    # 转换 tool_calls → Anthropic tool_use blocks
                    blocks: list[dict[str, Any]] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        func = tc.get("function", tc)
                        arguments = func.get("arguments", "{}")
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                arguments = {"_raw": arguments}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": func["name"],
                            "input": arguments,
                        })
                    anthropic_messages.append({"role": "assistant", "content": blocks})
                elif content:
                    anthropic_messages.append({"role": "assistant", "content": content})

            elif role == "tool":
                # OpenAI tool result → Anthropic tool_result
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg["tool_call_id"],
                        "content": msg["content"],
                    }],
                })

        # 结构化输出：通过 assistant prefill 强制 JSON 输出
        if response_format and response_format.get("type") in ("json_object", "json_schema"):
            # 追加 assistant prefill 引导 JSON 输出
            anthropic_messages.append({
                "role": "assistant",
                "content": "{",
            })

        return system_prompt, anthropic_messages

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """调用 DeepSeek Anthropic API 的流式接口"""

        system_prompt, anthropic_messages = self._convert_messages(messages, response_format)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }

        # temperature 不在 Anthropic SDK stream() 签名中，通过 extra_body 传入
        if response_format and response_format.get("type") in ("json_object", "json_schema"):
            kwargs["extra_body"] = {"temperature": min(temperature, 0.3)}
        else:
            kwargs["extra_body"] = {"temperature": temperature}

        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": "auto"}

        # Thinking 模式
        if self._enable_thinking:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget_tokens,
            }

        # 使用 Anthropic SDK 的流式接口
        input_tokens = 0
        output_tokens = 0
        finish_reason = "stop"
        tool_call_buffers: list[dict[str, Any]] = []
        current_tool_input = ""
        current_tool_id = ""
        current_tool_name = ""
        in_tool_use = False
        has_prefill = response_format and response_format.get("type") in ("json_object", "json_schema")

        # prefill 的 "{" 需要作为第一个 text chunk 发出
        if has_prefill:
            yield TextChunk(content="{")

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                # --- content_block_start ---
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        in_tool_use = True
                        current_tool_id = block.id
                        current_tool_name = block.name
                        current_tool_input = ""
                    elif block.type == "thinking":
                        # thinking block — 我们不输出 thinking 内容到用户
                        pass

                # --- content_block_delta ---
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield TextChunk(content=delta.text)
                    elif delta.type == "input_json_delta":
                        current_tool_input += delta.partial_json
                    elif delta.type == "thinking_delta":
                        # 思考过程，不输出
                        pass

                # --- content_block_stop ---
                elif event.type == "content_block_stop":
                    if in_tool_use:
                        # 工具调用完成
                        try:
                            args = json.loads(current_tool_input) if current_tool_input else {}
                        except json.JSONDecodeError:
                            args = {"_raw": current_tool_input}
                        tool_call_buffers.append({
                            "id": current_tool_id,
                            "name": current_tool_name,
                            "arguments": args,
                        })
                        in_tool_use = False

                # --- message_delta (final) ---
                elif event.type == "message_delta":
                    if hasattr(event, "usage") and event.usage:
                        output_tokens = event.usage.output_tokens or 0
                    if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                        sr = event.delta.stop_reason
                        if sr == "end_turn":
                            finish_reason = "stop"
                        elif sr == "tool_use":
                            finish_reason = "tool_calls"
                        elif sr == "max_tokens":
                            finish_reason = "length"
                        else:
                            finish_reason = sr or "stop"

                # --- message_start (usage) ---
                elif event.type == "message_start":
                    if hasattr(event, "message") and hasattr(event.message, "usage"):
                        input_tokens = event.message.usage.input_tokens or 0

        # 输出完整的 tool_calls
        for buf in tool_call_buffers:
            yield ToolCallChunk(
                id=buf["id"],
                name=buf["name"],
                arguments=buf["arguments"],
            )

        yield StreamDone(
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """调用 DeepSeek Anthropic API 的非流式接口"""

        system_prompt, anthropic_messages = self._convert_messages(messages, response_format)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }

        # temperature 不在 Anthropic SDK create() 签名中，通过 extra_body 传入
        if response_format and response_format.get("type") in ("json_object", "json_schema"):
            kwargs["extra_body"] = {"temperature": min(temperature, 0.3)}
        else:
            kwargs["extra_body"] = {"temperature": temperature}

        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": "auto"}

        # Thinking 模式
        if self._enable_thinking:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget_tokens,
            }

        response = await self._client.messages.create(**kwargs)

        # 解析 response
        content_text = ""
        tool_calls: list[ToolCallChunk] = []
        has_prefill = response_format and response_format.get("type") in ("json_object", "json_schema")

        if has_prefill:
            content_text = "{"

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCallChunk(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))
            # thinking block — 不输出

        # finish_reason 转换
        stop_reason = response.stop_reason
        if stop_reason == "end_turn":
            finish_reason = "stop"
        elif stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = stop_reason or "stop"

        input_tokens = response.usage.input_tokens or 0
        output_tokens = response.usage.output_tokens or 0

        return CompletionResult(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
