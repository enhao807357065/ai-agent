"""Gateway 对外 HTTP / SSE 协议的类型化响应模型。

这些模型描述网关向不同客户端协议编码后的 wire contract；它们与
``GatewayModelResult``（路由层内部结果）分层，避免协议细节反向污染 Router。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OpenAIModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default=0, ge=0)
    owned_by: str


class OpenAIModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[OpenAIModelInfo]


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------

class OpenAIUsage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class OpenAIFunctionCallPayload(BaseModel):
    name: str
    arguments: str


class OpenAIChatToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: OpenAIFunctionCallPayload


class OpenAIChatMessageResponse(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[OpenAIChatToolCall] | None = None


class OpenAIChatChoice(BaseModel):
    index: int = Field(ge=0)
    message: OpenAIChatMessageResponse
    finish_reason: Literal["stop", "tool_calls", "length"]


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(ge=0)
    model: str
    choices: list[OpenAIChatChoice] = Field(min_length=1)
    usage: OpenAIUsage


class OpenAIChatDelta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[OpenAIChatToolCall] | None = None


class OpenAIChatChunkChoice(BaseModel):
    index: int = Field(ge=0)
    delta: OpenAIChatDelta
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None


class OpenAIChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(ge=0)
    model: str
    choices: list[OpenAIChatChunkChoice] = Field(min_length=1)
    usage: OpenAIUsage | None = None


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------

class ResponsesOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str
    annotations: list[Any] = Field(default_factory=list)


class ResponsesMessageItem(BaseModel):
    type: Literal["message"] = "message"
    id: str
    role: Literal["assistant"] = "assistant"
    status: Literal["completed"] = "completed"
    content: list[ResponsesOutputText] = Field(min_length=1)


class ResponsesFunctionCallItem(BaseModel):
    type: Literal["function_call"] = "function_call"
    id: str
    call_id: str
    name: str
    arguments: str
    status: Literal["completed"] = "completed"


ResponsesOutputItem = ResponsesMessageItem | ResponsesFunctionCallItem


class ResponsesUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ResponsesResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created_at: int = Field(ge=0)
    status: Literal["completed"] = "completed"
    model: str
    output: list[ResponsesOutputItem] = Field(default_factory=list)
    output_text: str = ""
    usage: ResponsesUsage


class ResponsesCreatedEvent(BaseModel):
    type: Literal["response.created"] = "response.created"
    response: "ResponsesInProgressResponse"


class ResponsesInProgressResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    status: Literal["in_progress"] = "in_progress"
    model: str


class ResponsesOutputTextDeltaEvent(BaseModel):
    type: Literal["response.output_text.delta"] = "response.output_text.delta"
    response_id: str
    item_id: str
    output_index: int = Field(ge=0)
    content_index: int = Field(ge=0)
    delta: str


class ResponsesOutputItemDoneEvent(BaseModel):
    type: Literal["response.output_item.done"] = "response.output_item.done"
    response_id: str
    output_index: int = Field(ge=0)
    item: ResponsesFunctionCallItem


class ResponsesCompletedEvent(BaseModel):
    type: Literal["response.completed"] = "response.completed"
    response: ResponsesResponse


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

class AnthropicUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AnthropicTextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AnthropicToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


AnthropicContentBlock = AnthropicTextBlock | AnthropicToolUseBlock


class AnthropicMessageResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[AnthropicContentBlock] = Field(default_factory=list)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"] | None = None
    stop_sequence: None = None
    usage: AnthropicUsage


class AnthropicMessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: AnthropicMessageResponse


class AnthropicContentBlockStartEvent(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int = Field(ge=0)
    content_block: AnthropicContentBlock


class AnthropicTextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class AnthropicInputJsonDelta(BaseModel):
    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str


class AnthropicContentBlockDeltaEvent(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int = Field(ge=0)
    delta: AnthropicTextDelta | AnthropicInputJsonDelta


class AnthropicContentBlockStopEvent(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int = Field(ge=0)


class AnthropicMessageDeltaPayload(BaseModel):
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"]
    stop_sequence: None = None


class AnthropicMessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    delta: AnthropicMessageDeltaPayload
    usage: AnthropicUsage


class AnthropicMessageStopEvent(BaseModel):
    type: Literal["message_stop"] = "message_stop"
