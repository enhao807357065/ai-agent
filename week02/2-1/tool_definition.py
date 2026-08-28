from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ConfigDict
from typing import Literal, Callable, Awaitable
from execution_context import ExecutionContext, ToolError

Handler = Callable[
    [BaseModel, "ExecutionContext"],    # 加引号后，不会因为定义位置变化而立刻报错。
    Awaitable[BaseModel]
]
Permission = Literal["order:read", "order:write"]
RiskLevel = Literal["low", "medium", "high"]

# dataclass的必填字段必须在有默认值字段的前面
@dataclass(frozen=True)
class ToolDefinition:
    # ===== 必填：一个工具最小可执行契约 =====
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler

    # ===== 给 LLM 的行为边界 =====
    when_to_use: str = ""
    when_not_to_use: str = ""

    # 默认按“安全的只读工具”处理
    # side_effect: SideEffect = "none"
    requires_confirmation: bool = False
    idempotency: bool = True
    version: str = "v1"

    # ===== 授权与风险 =====
    # 注意：None 表示“该工具不要求特定 permission”，
    # 绝不能默认给 "order:read" 之类业务权限。
    permission: Permission | None = None
    risk: RiskLevel = "low"

    # ===== Runtime 治理 =====
    timeout_seconds: int = 10
    max_retries: int = 0

    # 当前还没有真正定义 RetryPolicy，先允许为空。
    # 后续可替换为 retry_policy: RetryPolicy | None = None
    retry_policy: type[BaseModel] | None = None

    # 统一的错误序列化模型
    error_model: type[BaseModel] = ToolError

    # 审计默认开启；生产环境通常不应默认关闭
    audit_log: bool = True

    # 默认不缓存，避免把用户/租户数据错误复用
    result_cache_policy: bool = False

    # None 表示“本工具不额外限制，由 Runtime 的全局并发控制”
    concurrency_limit: int | None = None

    def to_model_tools(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            }
        }