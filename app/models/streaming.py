"""
StreamingModel — 抽象流式模型接口

设计思路：
    Agent loop 不直接依赖 OpenAI SDK，而是通过 StreamingModel 抽象层交互。
    这样可以：
    1. 轻松切换不同 LLM 提供商（OpenAI、DeepSeek、本地模型）
    2. 测试时 mock 模型输出
    3. 未来接入非 OpenAI 协议的模型（如 Anthropic）

    StreamingModel 只关心"给一组 messages + tools，逐 token 产出文本或 tool_calls"。
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
# 抽象基类
# ============================================================

class StreamingModel(ABC):
    """流式模型抽象接口"""

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
    ) -> AsyncIterator[StreamChunk]:
        """
        流式调用模型（async generator）

        Args:
            messages: OpenAI 格式的消息列表
            tools: OpenAI 格式的 tools 定义（可选）
            temperature: 生成温度
            max_tokens: 最大输出 token

        Yields:
            TextChunk | ToolCallChunk | StreamDone
        """
        ...
