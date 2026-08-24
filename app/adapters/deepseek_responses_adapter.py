"""
DeepSeek Responses API 格式的 StreamingModel 实现

使用 OpenAI SDK 直连 https://api.deepseek.com (Responses API)
参考文档：https://api-docs.deepseek.com/zh-cn/guides/responses_api

支持特性：
    - 流式输出（stream=True，语义化 SSE 事件）
    - 结构化输出（text.format → json_schema）
    - Tool Calls（OpenAI Responses 格式 function_call）
    - Thinking / Reasoning（通过 reasoning.effort 参数控制）

与 Chat Completions / Anthropic adapter 的主要差异：
    - 接口：client.responses.create() 而非 chat.completions.create()
    - 对话格式：instructions + input items（非 messages 列表）
    - 流式格式：语义化 SSE events（非 delta chunk）
    - Tool result：function_call_output item（非 role:"tool" message）
    - 无状态：多轮对话需客户端回传完整历史
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from app.models.streaming import (
    StreamingModel, TextChunk, ToolCallChunk, StreamDone, StreamChunk, CompletionResult,
)


class DeepSeekResponsesModel(StreamingModel):
    """DeepSeek Responses API 流式模型"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        reasoning_effort: str | None = None,
    ):
        self._model = model
        # "none" | "low" | "medium" | "high" | None
        self._reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
            max_retries=1,
        )

    @property
    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # 格式转换
    # ------------------------------------------------------------------

    def _convert_messages_to_input(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """
        将 OpenAI Chat 格式 messages 转换为 Responses API 的 instructions + input

        返回 (instructions, input_items)

        Responses API input 格式：
            - 简单字符串（单轮简单对话）
            - 或 item 列表：
              [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
              以及 function_call / function_call_output 类型 items
        """
        instructions = None
        input_items: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]

            if role == "system":
                # 合并 system 消息为 instructions
                if instructions is None:
                    instructions = msg["content"]
                else:
                    instructions += "\n\n" + msg["content"]

            elif role == "user":
                content = msg["content"]
                input_items.append({"role": "user", "content": content})

            elif role == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")

                if tool_calls:
                    # assistant 带 tool_calls → 先输出文本 item，再输出 function_call items
                    if content:
                        input_items.append({"role": "assistant", "content": content})
                    for tc in tool_calls:
                        func = tc.get("function", tc)
                        arguments = func.get("arguments", "{}")
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments)
                        input_items.append({
                            "type": "function_call",
                            "id": tc["id"],
                            "name": func["name"],
                            "arguments": arguments,
                        })
                elif content:
                    input_items.append({"role": "assistant", "content": content})

            elif role == "tool":
                # tool result → function_call_output
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg["tool_call_id"],
                    "output": msg["content"],
                })

        return instructions, input_items

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将 OpenAI Chat 格式的 tools 转为 Responses API 格式

        Chat: [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        Responses: [{"type": "function", "name": ..., "description": ..., "parameters": ...}]
        """
        responses_tools = []
        for tool in tools:
            func = tool.get("function", tool)
            responses_tools.append({
                "type": "function",
                "name": func["name"],
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return responses_tools

    def _build_input_kwarg(self, input_items: list[dict[str, Any]]) -> Any:
        """
        构造 input 参数：单条 user 文本消息时用简单字符串，否则用 item 列表
        """
        if len(input_items) == 1 and input_items[0].get("role") == "user":
            content = input_items[0]["content"]
            if isinstance(content, str):
                return content
        return input_items

    # ------------------------------------------------------------------
    # 流式接口
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """调用 DeepSeek Responses API 的流式接口"""

        instructions, input_items = self._convert_messages_to_input(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": self._build_input_kwarg(input_items),
            "stream": True,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }

        if instructions:
            kwargs["instructions"] = instructions

        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # Reasoning 配置
        if self._reasoning_effort and self._reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": self._reasoning_effort}

        # 结构化输出
        if response_format:
            fmt_type = response_format.get("type")
            if fmt_type == "json_object":
                kwargs["text"] = {"format": {"type": "json_object"}}
            elif fmt_type == "json_schema":
                schema = response_format.get("json_schema", {})
                kwargs["text"] = {"format": {
                    "type": "json_schema",
                    "name": schema.get("name", "response"),
                    "schema": schema.get("schema", {}),
                }}

        # 调用 Responses API 流式接口
        input_tokens = 0
        output_tokens = 0
        finish_reason = "stop"
        tool_call_buffers: list[dict[str, Any]] = []
        current_fc: dict[str, Any] | None = None

        stream = await self._client.responses.create(**kwargs)

        async for event in stream:
            event_type = event.type

            # 文本 delta
            if event_type == "response.output_text.delta":
                yield TextChunk(content=event.delta)

            # function_call 参数 delta（流式拼装）
            elif event_type == "response.function_call_arguments.delta":
                if current_fc is not None:
                    current_fc["arguments"] += event.delta

            # output_item 开始 — 检测 function_call 类型
            elif event_type == "response.output_item.added":
                item = event.item
                if hasattr(item, "type") and item.type == "function_call":
                    current_fc = {
                        "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                        "arguments": "",
                    }

            # output_item 完成
            elif event_type == "response.output_item.done":
                item = event.item
                if hasattr(item, "type") and item.type == "function_call":
                    if current_fc:
                        tool_call_buffers.append(current_fc)
                        current_fc = None
                    else:
                        # fallback: 从 done item 中提取完整数据
                        args_str = getattr(item, "arguments", "{}")
                        try:
                            args = json.loads(args_str) if args_str else {}
                        except (json.JSONDecodeError, TypeError):
                            args = {"_raw": str(args_str)}
                        tool_call_buffers.append({
                            "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                            "name": getattr(item, "name", ""),
                            "arguments": args,
                        })

            # 响应完成 — 提取 usage 和 finish_reason
            elif event_type == "response.completed":
                resp = event.response
                if hasattr(resp, "usage") and resp.usage:
                    input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
                    output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
                status = getattr(resp, "status", "completed")
                if status == "completed":
                    finish_reason = "tool_calls" if tool_call_buffers else "stop"
                elif status == "incomplete":
                    finish_reason = "length"
                else:
                    finish_reason = "stop"

            elif event_type == "response.incomplete":
                finish_reason = "length"

            elif event_type == "response.failed":
                finish_reason = "stop"

        # 流结束后，输出完整的 tool_calls
        for buf in tool_call_buffers:
            args = buf["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
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

    # ------------------------------------------------------------------
    # 非流式接口
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """调用 DeepSeek Responses API 的非流式接口"""

        instructions, input_items = self._convert_messages_to_input(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": self._build_input_kwarg(input_items),
            "stream": False,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }

        if instructions:
            kwargs["instructions"] = instructions

        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # Reasoning 配置
        if self._reasoning_effort and self._reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": self._reasoning_effort}

        # 结构化输出
        if response_format:
            fmt_type = response_format.get("type")
            if fmt_type == "json_object":
                kwargs["text"] = {"format": {"type": "json_object"}}
            elif fmt_type == "json_schema":
                schema = response_format.get("json_schema", {})
                kwargs["text"] = {"format": {
                    "type": "json_schema",
                    "name": schema.get("name", "response"),
                    "schema": schema.get("schema", {}),
                }}

        response = await self._client.responses.create(**kwargs)

        # 解析 response.output_text
        content_text = getattr(response, "output_text", "") or ""

        # 从 output items 中提取 function_call
        tool_calls: list[ToolCallChunk] = []
        for item in getattr(response, "output", []):
            if getattr(item, "type", "") == "function_call":
                args_str = getattr(item, "arguments", "{}")
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": str(args_str)}
                tool_calls.append(ToolCallChunk(
                    id=getattr(item, "call_id", "") or getattr(item, "id", ""),
                    name=getattr(item, "name", ""),
                    arguments=args,
                ))

        # finish_reason
        status = getattr(response, "status", "completed")
        if status == "completed":
            finish_reason = "tool_calls" if tool_calls else "stop"
        elif status == "incomplete":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        # token 用量
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0 if usage else 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0 if usage else 0

        return CompletionResult(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
