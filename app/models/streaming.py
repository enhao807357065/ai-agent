"""
StreamingModel — 抽象模型接口（流式 + 非流式）

设计思路：
    Agent loop 不直接依赖 OpenAI/Anthropic SDK，而是通过 StreamingModel 抽象层交互。
    这样可以：
    1. 轻松切换不同 LLM 提供商（OpenAI、DeepSeek、本地模型）
    2. 测试时 mock 模型输出
    3. 未来接入非 OpenAI 协议的模型（如 Anthropic）

    两种调用方式：
    - stream(): 流式输出，async generator yield TextChunk/ToolCallChunk/StreamDone
    - complete(): 非流式输出，一次性返回完整 CompletionResult
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


# ============================================================
# 流式输出的 Chunk 类型
# ============================================================

@dataclass
class TextChunk:
    """文本 delta"""
    content: str


@dataclass
class ToolCallChunk:
    """工具调用（完整的一个 tool_call，非流式拼装）"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StreamDone:
    """流结束标记"""
    finish_reason: str  # "stop" | "tool_calls" | "length"
    input_tokens: int = 0
    output_tokens: int = 0


# Union type for stream chunks
StreamChunk = TextChunk | ToolCallChunk | StreamDone


# ============================================================
# 非流式输出的返回类型
# ============================================================

@dataclass
class CompletionResult:
    """complete() 的返回结果"""
    content: str
    tool_calls: list[ToolCallChunk] = field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0


# ============================================================
# 抽象基类
# ============================================================

class StreamingModel(ABC):
    """模型抽象接口（流式 + 非流式）"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """返回模型标识"""
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        流式调用模型（async generator）

        Args:
            messages: OpenAI 格式的消息列表
            tools: OpenAI 格式的 tools 定义（可选）
            temperature: 生成温度
            max_tokens: 最大输出 token
            response_format: 结构化输出格式（可选）
                - {"type": "json_object"} — 强制 JSON 输出
                - {"type": "json_schema", "json_schema": {...}} — JSON Schema 约束

        Yields:
            TextChunk | ToolCallChunk | StreamDone
        """
        ...

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """
        非流式调用模型（一次性返回完整结果）

        Args:
            messages: OpenAI 格式的消息列表
            tools: OpenAI 格式的 tools 定义（可选）
            temperature: 生成温度
            max_tokens: 最大输出 token
            response_format: 结构化输出格式（可选）

        Returns:
            CompletionResult 包含完整文本、tool_calls、token 用量
        """
        ...

