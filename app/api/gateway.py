"""OpenAI / Responses / Anthropic 兼容网关。

所有外部协议先转换为项目内部统一的 OpenAI Chat messages + tools，
再通过 StreamingModel 调用当前配置的上游模型。这样路由层不绑定 TAL、
DeepSeek Anthropic 或 DeepSeek Responses 等任一上游协议。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.adapters import create_model
from app.core.config import settings
from app.models.schemas import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ResponsesRequest,
)
from app.models.streaming import CompletionResult, StreamDone, TextChunk, ToolCallChunk

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["LLM Gateway"])


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> int:
    return int(time.time())


def _sse(data: dict[str, Any], event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _openai_tools_to_internal(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Chat 格式已是内部格式；Responses 工具仅需补一层 function。"""
    if not tools:
        return None
    result: list[dict[str, Any]] = []
    for tool in tools:
        if "function" in tool:
            result.append(tool)
        elif tool.get("type") == "function":
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        else:
            raise HTTPException(400, "Only function tools are supported")
    return result


def _anthropic_tools_to_internal(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


def _anthropic_messages_to_internal(
    system: str | list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Anthropic Messages → 内部 OpenAI Chat messages。"""
    converted: list[dict[str, Any]] = []
    if system:
        if isinstance(system, str):
            system_text = system
        else:
            system_text = "\n".join(
                block.get("text", "") for block in system if block.get("type") == "text"
            )
        if system_text:
            converted.append({"role": "system", "content": system_text})

    for message in messages:
        role = message["role"]
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    })
            item: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                item["tool_calls"] = tool_calls
            converted.append(item)
        elif role == "user":
            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    converted.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block.get("content", ""),
                    })
            if text_parts:
                converted.append({"role": "user", "content": "".join(text_parts)})
        else:
            raise HTTPException(400, f"Unsupported Anthropic message role: {role}")
    return converted


def _responses_input_to_internal(
    instructions: str | None,
    input_value: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Responses input items → 内部 OpenAI Chat messages。"""
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages

    pending_tool_calls: list[dict[str, Any]] = []
    for item in input_value:
        item_type = item.get("type")
        if item_type == "function_call":
            pending_tool_calls.append({
                "id": item.get("call_id") or item.get("id") or _id("call"),
                "type": "function",
                "function": {"name": item["name"], "arguments": item.get("arguments", "{}")},
            })
            continue
        if item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item["call_id"],
                "content": item.get("output", ""),
            })
            continue
        role = item.get("role")
        if role not in {"user", "assistant", "system", "developer"}:
            raise HTTPException(400, f"Unsupported Responses input item: {item_type or role}")
        if pending_tool_calls:
            messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
            pending_tool_calls = []
        messages.append({"role": "system" if role == "developer" else role, "content": item.get("content", "")})
    if pending_tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    return messages


def _usage(result: CompletionResult | StreamDone) -> dict[str, int]:
    return {
        "prompt_tokens": result.input_tokens,
        "completion_tokens": result.output_tokens,
        "total_tokens": result.input_tokens + result.output_tokens,
    }


def _finish_reason(reason: str) -> str:
    return "tool_calls" if reason == "tool_calls" else reason


@router.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """OpenAI-compatible models 列表；返回当前网关配置的默认模型。"""
    model = create_model()
    return {
        "object": "list",
        "data": [{
            "id": model.model_name,
            "object": "model",
            "created": 0,
            "owned_by": "ai-agent",
        }],
    }


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    """OpenAI Chat Completions 兼容接口（支持 function tools 与 SSE）。"""
    messages = [message.model_dump(exclude_none=True) for message in body.messages]
    model = create_model(body.model)
    stream = body.stream
    kwargs = {
        "messages": messages,
        "tools": _openai_tools_to_internal([tool.model_dump() for tool in body.tools] if body.tools else None),
        "temperature": body.temperature,
        "max_tokens": body.max_tokens or body.max_completion_tokens or 4096,
        "response_format": body.response_format,
    }
    completion_id = _id("chatcmpl")
    created = _now()
    logger.info("gateway.chat_completion", model=model.model_name, stream=stream)

    if not stream:
        result = await model.complete(**kwargs)
        message: dict[str, Any] = {"role": "assistant", "content": result.content or None}
        if result.tool_calls:
            message["tool_calls"] = [{
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            } for call in result.tool_calls]
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model.model_name,
            "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(result.finish_reason)}],
            "usage": _usage(result),
        }

    async def generate() -> AsyncIterator[str]:
        role_sent = False
        async for chunk in model.stream(**kwargs):
            if isinstance(chunk, TextChunk):
                delta: dict[str, Any] = {"content": chunk.content}
                if not role_sent:
                    delta["role"] = "assistant"
                    role_sent = True
                yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                            "model": model.model_name, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            elif isinstance(chunk, ToolCallChunk):
                yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                            "model": model.model_name, "choices": [{"index": 0, "delta": {"tool_calls": [{
                                "index": 0, "id": chunk.id, "type": "function",
                                "function": {"name": chunk.name, "arguments": json.dumps(chunk.arguments, ensure_ascii=False)},
                            }]}, "finish_reason": None}]})
            elif isinstance(chunk, StreamDone):
                yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                            "model": model.model_name, "choices": [{"index": 0, "delta": {},
                            "finish_reason": _finish_reason(chunk.finish_reason)}], "usage": _usage(chunk)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/responses")
async def responses(body: ResponsesRequest):
    """OpenAI Responses 兼容接口（无状态；客户端每轮传完整 input）。"""
    model = create_model(body.model)
    stream = body.stream
    input_value = body.input if isinstance(body.input, str) else [item.model_dump(exclude_none=True) for item in body.input]
    messages = _responses_input_to_internal(body.instructions, input_value)
    response_format = body.text.format if body.text else None
    kwargs = {
        "messages": messages,
        "tools": _openai_tools_to_internal([tool.model_dump() for tool in body.tools] if body.tools else None),
        "temperature": body.temperature,
        "max_tokens": body.max_output_tokens,
        "response_format": response_format,
    }
    response_id = _id("resp")
    created = _now()
    logger.info("gateway.responses", model=model.model_name, stream=stream)

    def output_from(result: CompletionResult) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if result.content:
            output.append({"type": "message", "id": _id("msg"), "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": result.content, "annotations": []}]})
        for call in result.tool_calls:
            output.append({"type": "function_call", "id": _id("fc"), "call_id": call.id,
                           "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False), "status": "completed"})
        return output

    if not stream:
        result = await model.complete(**kwargs)
        return {
            "id": response_id, "object": "response", "created_at": created, "status": "completed",
            "model": model.model_name, "output": output_from(result), "output_text": result.content,
            "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                      "total_tokens": result.input_tokens + result.output_tokens},
        }

    async def generate() -> AsyncIterator[str]:
        yield _sse({"type": "response.created", "response": {"id": response_id, "object": "response", "status": "in_progress", "model": model.model_name}}, "response.created")
        text_index = 0
        async for chunk in model.stream(**kwargs):
            if isinstance(chunk, TextChunk):
                yield _sse({"type": "response.output_text.delta", "response_id": response_id,
                            "item_id": "msg_gateway", "output_index": 0, "content_index": 0,
                            "delta": chunk.content}, "response.output_text.delta")
            elif isinstance(chunk, ToolCallChunk):
                yield _sse({"type": "response.output_item.done", "response_id": response_id,
                            "output_index": text_index, "item": {"type": "function_call", "id": _id("fc"),
                            "call_id": chunk.id, "name": chunk.name,
                            "arguments": json.dumps(chunk.arguments, ensure_ascii=False), "status": "completed"}}, "response.output_item.done")
                text_index += 1
            elif isinstance(chunk, StreamDone):
                yield _sse({"type": "response.completed", "response": {"id": response_id, "object": "response",
                            "status": "completed", "model": model.model_name,
                            "usage": {"input_tokens": chunk.input_tokens, "output_tokens": chunk.output_tokens,
                            "total_tokens": chunk.input_tokens + chunk.output_tokens}}}, "response.completed")

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages")
async def anthropic_messages(body: AnthropicMessagesRequest):
    """Anthropic Messages 兼容接口（支持 tool_use / tool_result 和 Anthropic SSE）。"""
    messages = [message.model_dump() for message in body.messages]
    model = create_model(body.model)
    stream = body.stream
    kwargs = {
        "messages": _anthropic_messages_to_internal(body.system, messages),
        "tools": _anthropic_tools_to_internal([tool.model_dump() for tool in body.tools] if body.tools else None),
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "response_format": None,
    }
    message_id = _id("msg")
    logger.info("gateway.anthropic_messages", model=model.model_name, stream=stream)

    def blocks(result: CompletionResult) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if result.content:
            content.append({"type": "text", "text": result.content})
        content.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                       for call in result.tool_calls)
        return content

    if not stream:
        result = await model.complete(**kwargs)
        stop_reason = "tool_use" if result.tool_calls else "end_turn"
        if result.finish_reason == "length":
            stop_reason = "max_tokens"
        return {
            "id": message_id, "type": "message", "role": "assistant", "model": model.model_name,
            "content": blocks(result), "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        }

    async def generate() -> AsyncIterator[str]:
        yield _sse({"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant",
                    "model": model.model_name, "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}}, "message_start")
        text_started = False
        index = 0
        async for chunk in model.stream(**kwargs):
            if isinstance(chunk, TextChunk):
                if not text_started:
                    yield _sse({"type": "content_block_start", "index": index,
                                "content_block": {"type": "text", "text": ""}}, "content_block_start")
                    text_started = True
                yield _sse({"type": "content_block_delta", "index": index,
                            "delta": {"type": "text_delta", "text": chunk.content}}, "content_block_delta")
            elif isinstance(chunk, ToolCallChunk):
                if text_started:
                    yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                    index += 1
                    text_started = False
                yield _sse({"type": "content_block_start", "index": index, "content_block": {
                    "type": "tool_use", "id": chunk.id, "name": chunk.name, "input": {}}}, "content_block_start")
                arguments = json.dumps(chunk.arguments, ensure_ascii=False)
                yield _sse({"type": "content_block_delta", "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": arguments}}, "content_block_delta")
                yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                index += 1
            elif isinstance(chunk, StreamDone):
                if text_started:
                    yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                stop_reason = "tool_use" if chunk.finish_reason == "tool_calls" else "end_turn"
                if chunk.finish_reason == "length":
                    stop_reason = "max_tokens"
                yield _sse({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                            "usage": {"output_tokens": chunk.output_tokens}}, "message_delta")
        yield _sse({"type": "message_stop"}, "message_stop")

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
