"""LLM Gateway 的能力、候选目标与路由策略领域模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ModelCapability(StrEnum):
    CHAT = "chat"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    REASONING = "reasoning"


class TargetProfile(BaseModel):
    """一个可被 Gateway 选择的真实上游目标的静态声明。"""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._/-]+$")
    provider: Literal["talai", "deepseek", "deepseek_responses"]
    model: str = Field(min_length=1)
    capabilities: set[ModelCapability] = Field(default_factory=lambda: {ModelCapability.CHAT})
    priority: int = Field(default=100, ge=0)
    input_cost_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    output_cost_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    max_context_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    enabled: bool = True
    enable_thinking: bool | None = None
    thinking_budget_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class RoutingPolicy(BaseModel):
    """逻辑模型的候选池与静态业务路由策略，而非“最大模型优先”。"""

    target_ids: list[str] = Field(min_length=1)
    required_capabilities: set[ModelCapability] = Field(default_factory=set)
    selection_order: tuple[Literal["priority", "estimated_cost", "health"], ...] = (
        "priority", "estimated_cost", "health",
    )
    max_estimated_cost: Decimal | None = Field(default=None, ge=0)


class GatewayRequirements(BaseModel):
    """由归一化请求推导出的不可协商能力约束。"""

    capabilities: set[ModelCapability] = Field(default_factory=lambda: {ModelCapability.CHAT})
    min_output_tokens: int = Field(default=1, gt=0)


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"


class TargetHealth(BaseModel):
    target_id: str
    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    latency_ewma_ms: float | None = Field(default=None, ge=0)
    circuit_open_until: datetime | None = None


class CandidateTarget(BaseModel):
    """过滤后进入执行阶段的候选；理由保留给日志和测试。"""

    profile: TargetProfile
    estimated_cost: Decimal
    health_score: float = Field(ge=0, le=1)
