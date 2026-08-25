"""
API 请求/响应 Schema 定义
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ============================================================
# 请求模型
# ============================================================

class Message(BaseModel):
    """对话消息"""
    role: str = Field(..., description="角色: system / user / assistant / tool")
    content: str = Field(..., description="消息内容")
    tool_call_id: str | None = Field(default=None, description="工具调用 ID（tool 角色时使用）")


class ToolDefinition(BaseModel):
    """工具定义（传递给 LLM 的 function schema）"""
    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema 参数定义")


class CreateRunRequest(BaseModel):
    """
    创建 Run 的请求体

    两种用法：
      1. 不传 run_id → 新建会话
      2. 传 run_id → 在已有会话上继续对话（messages 追加到已有历史）
    """
    run_id: str | None = Field(default=None, description="会话 ID（传入则继续对话，不传则新建）")
    messages: list[Message] = Field(..., description="本次发送的消息（通常是一条 user 消息）")
    tools: list[ToolDefinition] = Field(default_factory=list, description="可用工具列表")
    model: str | None = Field(default=None, description="模型名（留空使用默认）")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="生成温度")
    max_turns: int = Field(default=10, ge=1, le=50, description="最大轮次（防无限循环）")
    system: str | None = Field(default=None, description="系统提示词（仅新建时生效）")
    stream: bool = Field(default=True, description="是否流式输出（False 时同步等待完整结果返回）")


# ============================================================
# 响应模型
# ============================================================

class RunStatus(str, Enum):
    """Run 状态枚举"""
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"


class RunInfo(BaseModel):
    """Run 基本信息（列表/详情接口返回）"""
    run_id: str
    status: RunStatus
    created_at: float
    completed_at: float | None = None
    model: str
    total_turns: int = 0
    error: str | None = None


class CancelRunResponse(BaseModel):
    """取消 Run 的响应"""
    run_id: str
    status: RunStatus
    message: str


# ============================================================
# LLM Gateway 请求模型
# ============================================================

class OpenAIFunction(BaseModel):
    """OpenAI function tool 的函数定义。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )


class OpenAITool(BaseModel):
    """OpenAI Chat Completions / Responses 的 function tool。"""

    type: Literal["function"] = "function"
    function: OpenAIFunction


class GatewayChatMessage(BaseModel):
    """网关接收的 OpenAI Chat message。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions 请求。"""

    model: str | None = None
    messages: list[GatewayChatMessage] = Field(min_length=1)
    tools: list[OpenAITool] | None = None
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    response_format: dict[str, Any] | None = None


class ResponsesInputItem(BaseModel):
    """OpenAI Responses API 的单个 input item。"""

    type: str | None = None
    role: Literal["user", "assistant", "system", "developer"] | None = None
    content: str | list[dict[str, Any]] | None = None
    id: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None


class ResponsesTextConfig(BaseModel):
    """Responses API 的 text 配置。"""

    format: dict[str, Any] | None = None


class ResponsesRequest(BaseModel):
    """POST /v1/responses 请求。"""

    model: str | None = None
    instructions: str | None = None
    input: str | list[ResponsesInputItem]
    tools: list[OpenAITool] | None = None
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, gt=0)
    text: ResponsesTextConfig | None = None


class AnthropicTool(BaseModel):
    """Anthropic Messages API 的 tool 定义。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )


class AnthropicMessage(BaseModel):
    """Anthropic Messages API 的 message。"""

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class AnthropicMessagesRequest(BaseModel):
    """POST /v1/messages 请求。"""

    model: str | None = None
    max_tokens: int = Field(gt=0)
    messages: list[AnthropicMessage] = Field(min_length=1)
    system: str | list[dict[str, Any]] | None = None
    tools: list[AnthropicTool] | None = None
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
