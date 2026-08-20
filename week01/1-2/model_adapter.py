from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

ResultKind = Literal["text", "structured", "tool_calls", "refusal"]

# dataclass自动生成init/repr/eq等样板代码，如果需要生成__hash__，需要加上frozen=True
# frozen=True 不可变（生成 __hash__，禁止赋值）
@dataclass(frozen=True)
class ModelCapabilities:
    chat_completions: bool
    responses: bool
    structured_output: Literal["native_schema", "json_mode", "prompt_only"]
    tool_calling: bool
    supports_temperature: bool
    supports_top_p: bool


@dataclass(frozen=True)
class ModelRequest:
    system: str
    user: str
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int = 1024
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelResult(BaseModel):
    kind: ResultKind
    text: str | None = None
    data: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None


class ModelAdapter(ABC):
    name: str
    capabilities: ModelCapabilities

    # abstractmethod 的方法体不需要实现，用 pass、raise NotImplementedError、或写个 docstring 都可以。但选择哪种写法，反映了不同的设计意图。
    # 防御性编程：如果有人"意外"调用到了，立即报错
    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResult:
        return NotImplementedError