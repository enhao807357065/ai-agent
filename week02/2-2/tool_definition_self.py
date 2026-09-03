from collections.abc import Callable
from typing import Literal, Awaitable, Any, Mapping
from pydantic import BaseModel, ConfigDict, ValidationError
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

class BaseToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class BaseToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class GetWeatherInput(BaseToolInput):
    city: str
    date: datetime

class GetWeatherOutput(BaseToolOutput):
    temperature: float
    weather_detail: str

@dataclass(frozen=True)
class ExecutionContext:
    user_id: str
    tenant_id: str
    permission: frozenset[str]
    approved_call_ids: frozenset[str] = frozenset()

Handler = Callable[[BaseModel, ExecutionContext], Awaitable[dict[str, Any]]]

class RetryPolicy:
    max_retry_num: int
    retry_on_timeout: bool

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    error_model: type[BaseModel]

    permission: str
    timeout: float
    risk: Literal["low", "medium", "high"]
    handler: Handler
    retries: RetryPolicy
    category: Literal["database", "http", "file", "external"]  # 工具的分类，便于隔离与策略
    access: Literal["read", "write"]    # 影响并发与确认策略
    dependencies: tuple[str, ...] = ()  # 依赖的其他下游服务，需要下游服务健康才能继续执行

    def tool_provider_tool(self):
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema()
        }

@dataclass(frozen=True)
class ToolSnapshot:
    tools: Mapping[str, ToolDefinition]

    def provider_tools(self) -> list[dict[str, Any]]:
        return [
            tool.tool_provider_tool()
            for tool in self.tools.values()
        ]

class ToolRegistry:
    def __init__(self):
        self._tools: dict[tuple[(str, str)], ToolDefinition] = {}

    def register(self, tool: ToolDefinition):
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(f"工具注册重复：{key}")
        self._tools[key] = tool

    def snapshot(self, routes: dict[str, str], enable: dict[str, bool]) -> ToolSnapshot:
        selected: dict[str, ToolDefinition] = {}
        for name, version in routes:
            key = (name, version)
            if self._tools.get(key) is None:
                # 没有这个版本的工具，直接跳过
                raise ValueError(f"没有这个工具：{key}")
            if not enable.get(name):
                # 工具当前不是启用状态
                continue
            selected[name] = self._tools.get(key)
        return ToolSnapshot(MappingProxyType(selected))


class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class ToolMessageResult:
    tool_call_id: str
    tool_name: str
    content: list[dict[str, Any]]
    detail: dict[str, Any]
    is_error: bool
    error: dict[str, Any] | None = None

class ToolRuntime:
    def __init__(self, enabled: dict[str, bool]):
        self.enabled = enabled

    def _error(self, tool_call: ToolCall, ctx: ExecutionContext, tool: ToolDefinition, code: str) -> ToolMessageResult:
        return ToolMessageResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=[{"type": "text", "text": code}],
            detail={"error": code},
            is_error=True,
            error={"code": code}
        )

    async def invoke(self, snapshot: ToolSnapshot, tool_call: ToolCall, ctx: ExecutionContext) -> ToolMessageResult:
        # 校验工具是否可用
        tool = snapshot.tools.get(tool_call.name)
        if tool is None:
            return self._error(tool_call, ctx, snapshot, "TOOL INVALID")
        # 校验是否开启
        if not self.enabled.get(tool_call.name):
            return self._error(tool_call, ctx, snapshot, "TOOL DISABLED")

        # 入参格式校验
        try:
            params = tool.input_model.model_validate(tool_call.arguments)
        except ValidationError:
            return self._error(tool_call, ctx, snapshot, "MODEL VALIDATE")

        # 权限校验
        if tool.permission not in ctx.permission:
            return self._error(tool_call, ctx, snapshot, "NO PERMISSION")
        if tool.risk == "high" and tool_call.id not in ctx.approved_call_ids:
            return self._error(tool_call, ctx, snapshot, "NEED APPROVED")

        # 执行
        for attempt in range(1, tool.retries.max_retry_num+1):
            try:
                output_raw = await tool.handler(params, ctx)
                break
            except TimeoutError:
                return self._error(tool_call, ctx, snapshot, "TIMEOUT ERROR")

        # finalize：
        try:
            output = tool.output_model.model_validate(output_raw)
        except ValidationError:
            return self._error(tool_call, ctx, snapshot, "MODEL OUTPUT VALIDATE")

        # 审计、日志相关，先忽略

        return ToolMessageResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=[{"type": "text", "text": output}],
            is_error=False
        )