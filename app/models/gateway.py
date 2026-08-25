"""LLM Gateway 的逻辑模型路由领域模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProviderTarget(BaseModel):
    """一个真实上游目标；由逻辑模型路由选择，不暴露给调用方。"""

    provider: Literal["talai", "deepseek", "deepseek_responses"]
    model: str = Field(min_length=1)
    enabled: bool = True
    enable_thinking: bool | None = None
    thinking_budget_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class ModelRoute(BaseModel):
    """逻辑模型的 primary + fallback 路由策略。"""

    primary: ProviderTarget
    fallbacks: list[ProviderTarget] = Field(default_factory=list)


class GatewayModelCall(BaseModel):
    """协议层归一化后，交给模型路由层执行的调用命令。

    Router 只认识该领域对象，不接收协议层散落的 ``**kwargs``。
    """

    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    response_format: dict[str, Any] | None = None


class GatewayRouteInfo(BaseModel):
    """本次调用最终命中的上游路由，供网关内部观测与协议编码使用。"""

    logical_model: str
    provider: str
    upstream_model: str
    attempt: int = Field(ge=1)
    used_fallback: bool


class GatewayToolCall(BaseModel):
    """Gateway 统一的已完成工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


class GatewayUsage(BaseModel):
    """Gateway 统一 token 用量。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class GatewayModelResult(BaseModel):
    """Gateway Router 的归一化非流式输出。"""

    route: GatewayRouteInfo
    content: str = ""
    tool_calls: list[GatewayToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length"] = "stop"
    usage: GatewayUsage = Field(default_factory=GatewayUsage)


class GatewayTextDelta(BaseModel):
    """Gateway Router 的文本流事件。"""

    type: Literal["text.delta"] = "text.delta"
    content: str


class GatewayToolCallEvent(BaseModel):
    """Gateway Router 的工具调用流事件。"""

    type: Literal["tool_call.completed"] = "tool_call.completed"
    tool_call: GatewayToolCall


class GatewayCompletedEvent(BaseModel):
    """Gateway Router 的完成流事件；携带最终路由和用量。"""

    type: Literal["completed"] = "completed"
    finish_reason: Literal["stop", "tool_calls", "length"]
    usage: GatewayUsage
    route: GatewayRouteInfo


GatewayStreamEvent = GatewayTextDelta | GatewayToolCallEvent | GatewayCompletedEvent


# ---------------------------------------------------------------------------
# Gateway 对外统一 HTTP 契约。三个 URL 仅是路径别名，绝不代表不同协议。
# ---------------------------------------------------------------------------

class GatewayMessage(BaseModel):
    """Gateway 统一消息格式（内部 canonical message 的类型化版本）。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[GatewayToolCall] | None = None


class GatewayFunctionTool(BaseModel):
    """Gateway 唯一支持的 function tool 声明。"""

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )


class GatewayRequest(BaseModel):
    """所有 Gateway HTTP 路径共用的统一请求契约。"""

    model: str = Field(min_length=1, description="网关暴露的逻辑模型名")
    messages: list[GatewayMessage] = Field(min_length=1)
    tools: list[GatewayFunctionTool] | None = None
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    response_format: dict[str, Any] | None = None


class GatewayResponse(BaseModel):
    """所有 Gateway HTTP 路径共用的统一非流式响应契约。"""

    id: str
    object: Literal["gateway.response"] = "gateway.response"
    created: int = Field(ge=0)
    model: str
    content: str = ""
    tool_calls: list[GatewayToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length"]
    usage: GatewayUsage


class GatewayResponseTextDelta(BaseModel):
    """Gateway 统一 SSE 文本增量事件。"""

    type: Literal["gateway.text.delta"] = "gateway.text.delta"
    id: str
    model: str
    content: str


class GatewayResponseToolCallEvent(BaseModel):
    """Gateway 统一 SSE 工具调用完成事件。"""

    type: Literal["gateway.tool_call.completed"] = "gateway.tool_call.completed"
    id: str
    model: str
    tool_call: GatewayToolCall


class GatewayResponseCompletedEvent(BaseModel):
    """Gateway 统一 SSE 完成事件。不会暴露内部 provider route。"""

    type: Literal["gateway.completed"] = "gateway.completed"
    id: str
    model: str
    finish_reason: Literal["stop", "tool_calls", "length"]
    usage: GatewayUsage


class GatewayError(BaseModel):
    """网关公开错误语义；不泄露 provider、真实模型或 SDK 原始异常。"""

    code: Literal[
        "invalid_request",
        "invalid_model",
        "capability_unavailable",
        "structured_output_schema_invalid",
        "structured_output_invalid",
        "gateway_configuration_error",
        "upstream_request_error",
        "upstream_unavailable",
        "upstream_stream_interrupted",
        "internal_error",
    ]
    message: str
    retryable: bool = False


class GatewayErrorResponse(BaseModel):
    """非流式 Gateway 错误响应。"""

    object: Literal["gateway.error"] = "gateway.error"
    error: GatewayError


class GatewayResponseErrorEvent(BaseModel):
    """SSE 失败事件；发送后服务端立即结束当前流。"""

    type: Literal["gateway.error"] = "gateway.error"
    id: str
    model: str
    error: GatewayError


GatewayResponseStreamEvent = (
    GatewayResponseTextDelta
    | GatewayResponseToolCallEvent
    | GatewayResponseCompletedEvent
    | GatewayResponseErrorEvent
)


class GatewayModelInfo(BaseModel):
    id: str


class GatewayModelsResponse(BaseModel):
    object: Literal["gateway.model_list"] = "gateway.model_list"
    data: list[GatewayModelInfo]
