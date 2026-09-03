# 支持ToolSnapshot、ToolRegistry、ToolRuntime、ExecutionContext的工具调用
# Agent Run 创建
#     │
#     ├─ 控制面读取 route / enabled / tenant / rollout
#     │
#     └─ Registry 生成不可变 ToolSnapshot
#              │
#              └─ Snapshot 导出 LLM Provider tools
#
# LLM 返回 ToolCall（不可信）
#     │
#     ▼
# ToolRuntime.invoke(snapshot, call, ctx)
#     │
#     ├─ 1. 从 Snapshot 查询 tool
#     │      └─ 无 → TOOL_NOT_FOUND
#     │
#     ├─ 2. 实时 kill switch
#     │      └─ 关闭 → TOOL_DISABLED
#     │
#     ├─ 3. Pydantic 输入校验
#     │      └─ 失败 → INVALID_ARGUMENT
#     │
#     ├─ 4. Permission 检查
#     │      └─ 失败 → PERMISSION_DENIED
#     │
#     ├─ 5. Approval 检查
#     │      └─ 失败 → APPROVAL_REQUIRED
#     │
#     ├─ 6. Dependency / concurrency / rate-limit
#     │      └─ 失败 → DEPENDENCY_UNAVAILABLE 等
#     │
#     ├─ 7. timeout 包裹 handler 执行
#     │      └─ 仅幂等 transient 失败可 retry
#     │
#     ├─ 8. Pydantic 输出校验
#     │      └─ 失败 → INVALID_OUTPUT
#     │
#     ├─ 9. 写审计事件
#     │
#     └─ 10. 输出 JSON 字符串形式的 ToolMessageResult

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from dataclasses import dataclass
from typing import Literal, Any, Callable, Awaitable, Mapping
from types import MappingProxyType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateTicketInput(StrictModel):
    title: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)
    priority: Literal["low", "medium", "high"] = "medium"

class CreateTicketOutput(StrictModel):
    ticket_id: str
    status: Literal["created"]

class ToolCall(StrictModel):
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class ExecutionContext:
    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    approved_call_ids: frozenset[str] = frozenset()

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    retry_on_timeout: bool = False

ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[dict[str, Any]]]

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    error_codes: tuple[str, ...]
    handler: ToolHandler

    # 运行治理
    permission: str
    risk: Literal["low", "medium", "high"]
    timeout: float
    retry: RetryPolicy
    audit_fields: tuple[str, ...]   # 审计要记录的入参字段名
    category: Literal["database", "http", "file", "external"]  # 工具的分类，便于隔离与策略
    access: Literal["read", "write"]    # 影响并发与确认策略
    dependencies: tuple[str, ...] = ()  # 依赖的其他下游服务，需要下游服务健康才能继续执行
    execution_mode: Literal["sequential", "parallel"] = "parallel"  # 执行模式，串行或并行

    def to_provider_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema()
            }
        }

@dataclass(frozen=True)
class ToolSnapshot:
    tools: Mapping[str, ToolDefinition]

    def provider_tools(self) -> list[dict[str, Any]]:
        return [tool.to_provider_tool() for tool in self.tools.values()]

@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[dict[str, str]]
    details: dict[str, Any]
    is_error: bool
    error: dict[str, Any] | None = None

class ToolRegistry:
    def __init__(self):
        self._tools: dict[tuple[str, str], ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}@{tool.version}")
        if tool.timeout <= 0 or tool.retry.max_attempts <= 0:
            raise ValueError("timeout and retry policy must be positive")
        self._tools[key] = tool

    # 从全局注册表里，按当前路由配置裁一份只读工具集，给某次 Agent 运行用。
    # 创建 Snapshot 时读一次：控制“LLM 看得到什么”。这一步读取当前版本的开关状态，生成本次run的工具清单
    # 作用：
    # - 不把停用工具的 Schema 发送给模型；
    # - 避免模型尝试调用不可用工具；
    # - 让一次 Agent Run 的 Provider tool list 稳定；
    # - 支持按租户、环境、灰度组生成不同 Snapshot。
    def snapshot(self, routes: Mapping[str, str], enabled: Mapping[str, bool]) -> ToolSnapshot:
        selected: dict[str, ToolDefinition] = {}
        for name, version in routes.items():
            if not enabled.get(name, True):
                continue
            key = (name, version)
            if key not in self._tools:
                raise KeyError(f"tool not registered: {name}@{version}")
            selected[name] = self._tools[key]
        return ToolSnapshot(MappingProxyType(selected))

class ToolRunTime:
    def __init__(self, enabled: dict[str, bool], healthy_dependencies: set[str], audit_log: list[dict[str, Any]]):
        self.enabled = enabled
        self.healthy_dependencies = healthy_dependencies
        self.audit_log = audit_log

    def _error(
            self,
            call: ToolCall,
            context: ExecutionContext,
            tool: ToolDefinition | None,
            code: str,
    ) -> ToolResultMessage:
        if tool and code not in tool.error_codes:
            raise ValueError(f"undeclared error code: {code}")
        self._audit(call, context, tool, code)
        return ToolResultMessage(
            tool_call_id=call.id,
            tool_name=call.name,
            content=[{"type": "text", "text": code}],
            details={"version": tool.version if tool else None},
            is_error=True,
            error={"code": code, "retryable": code == "TIMEOUT"},
        )

    def _audit(
            self,
            call: ToolCall,
            context: ExecutionContext,
            tool: ToolDefinition | None,
            outcome: str,
    ) -> None:
        self.audit_log.append(
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "version": tool.version if tool else None,
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "outcome": outcome,
                "argument_fields": sorted(
                    name
                    for name in call.arguments
                    if tool and name in tool.audit_fields
                ),
            }
        )

    async def invoke(self, snapshot: ToolSnapshot, call: ToolCall, ctx: ExecutionContext) -> ToolResultMessage:
        tool = snapshot.tools.get(call.name)
        # 快照里没有工具
        if tool is None:
            return self._error(call, ctx, None, "TOOL_NOT_FOUND")
        # 工具当前状态是不可用的
        if not self.enabled.get(call.name, True):
            return self._error(call, ctx, tool, "TOOL_DISABLED")

        try:
            # 入参校验
            params = tool.input_model.model_validate(call.arguments)
        except ValidationError:
            return self._error(call, ctx, tool, "INVALID_ARGUMENT")

        # 权限检查
        if tool.permission not in ctx.permissions:
            return self._error(call, ctx, tool, "PERMISSION_DENIED")
        # 风险等级检查
        if tool.risk == "high" and call.id not in ctx.approved_call_ids:
            return self._error(call, ctx, tool, "APPROVAL_REQUIRED")
        # 下游服务健康检查，如果不健康也直接返回
        if any(name not in self.healthy_dependencies for name in tool.dependencies):
            return self._error(call, ctx, tool, "DEPENDENCY_UNAVAILABLE")

        # execute：只有 prepare 全部通过，handler 才能运行。
        for attempt in range(1, tool.retry.max_attempts + 1):
            try:
                raw_output = await asyncio.wait_for(
                    tool.handler(params, ctx),
                    timeout=tool.timeout,
                )
                break
            except TimeoutError:
                if not tool.retry.retry_on_timeout or attempt == tool.retry.max_attempts:
                    return self._error(call, ctx, tool, "TIMEOUT")

        # finalize：校验输出、审计，并包装成能写回 Agent Loop 的消息。
        try:
            output = tool.output_model.model_validate(raw_output)
        except ValidationError:
            return self._error(call, ctx, tool, "INVALID_OUTPUT")

        self._audit(call, ctx, tool, "OK")
        return ToolResultMessage(
            tool_call_id=call.id,
            tool_name=call.name,
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        output.model_dump(), ensure_ascii=False, sort_keys=True
                    ),
                }
            ],
            details={"version": tool.version, "attempt": attempt},
            is_error=False,
        )

async def create_ticket(params: BaseModel, context: ExecutionContext) -> dict[str, Any]:
    CreateTicketInput.model_validate(params)
    await asyncio.sleep(0.01)  # 模拟内部 HTTP API
    return {"ticket_id": "T-1001", "status": "created"}


#                  ┌────────────────────────────┐
#                  │ ToolDefinition Registry    │
#                  │ 工具契约、版本、Schema      │
#                  │ 通常随服务发布加载          │
#                  └──────────────┬─────────────┘
#                                 │
#                  ┌──────────────▼─────────────┐
#                  │ Tool Control Plane         │
#                  │ route / enabled / 灰度策略  │
#                  │ Redis / DB / 配置中心       │
#                  └──────────────┬─────────────┘
#                                 │
#        ┌────────────────────────┼────────────────────────┐
#        ▼                        ▼                        ▼
#   Agent Run A               Agent Run B               Agent Run C
#   创建 Snapshot             创建 Snapshot             创建 Snapshot


async def main() -> None:
    """打印一次端到端运行的详细过程，便于观察各层职责。"""
    error_codes = (
        "TOOL_DISABLED",
        "INVALID_ARGUMENT",
        "PERMISSION_DENIED",
        "APPROVAL_REQUIRED",
        "DEPENDENCY_UNAVAILABLE",
        "TIMEOUT",
        "INVALID_OUTPUT",
    )
    create_ticket_v1 = ToolDefinition(
        name="create_ticket",
        version="v1",
        description="在工单系统创建一张支持工单。",
        input_model=CreateTicketInput,
        output_model=CreateTicketOutput,
        error_codes=error_codes,
        handler=create_ticket,
        permission="ticket:write",
        risk="high",
        timeout=1.0,
        retry=RetryPolicy(max_attempts=1),
        audit_fields=("title", "priority"),
        category="http",
        access="write",
        dependencies=("ticket-api",),
        execution_mode="sequential",
    )

    registry = ToolRegistry()
    registry.register(create_ticket_v1)
    snapshot = registry.snapshot(
        routes={"create_ticket": "v1"},
        enabled={"create_ticket": True},
    )

    print("\n========== 1. ToolDefinition：导出 Provider Schema ==========")
    provider_tool = snapshot.provider_tools()[0]
    print(json.dumps(provider_tool, ensure_ascii=False, indent=2))

    print("\n========== 2. ToolSnapshot：版本冻结与只读映射 ==========")
    print(f"本次 Run Snapshot 中的版本: {snapshot.tools['create_ticket'].version}")
    create_ticket_v2 = ToolDefinition(
        **{**create_ticket_v1.__dict__, "version": "v2"}
    )
    registry.register(create_ticket_v2)
    print("Registry 后续注册了 create_ticket@v2")
    print(f"原 Snapshot 中的版本仍是: {snapshot.tools['create_ticket'].version}")
    try:
        snapshot.tools["other"] = create_ticket_v1  # type: ignore[index]
    except TypeError:
        print("尝试修改 snapshot.tools: TypeError（符合 MappingProxyType 的只读语义）")

    audit_log: list[dict[str, Any]] = []
    runtime = ToolRunTime(
        enabled={"create_ticket": True},
        healthy_dependencies={"ticket-api"},
        audit_log=audit_log,
    )
    context = ExecutionContext(
        user_id="user-1",
        tenant_id="tenant-1",
        permissions=frozenset({"ticket:write"}),
    )
    valid_arguments = {
        "title": "无法登录",
        "description": "登录后页面持续跳转。",
        "priority": "high",
    }

    print("\n========== 3. Runtime prepare：未确认的 high-risk 调用 ==========")
    pending_call = ToolCall(
        id="call-approval-required",
        name="create_ticket",
        arguments=valid_arguments,
    )
    pending = await runtime.invoke(snapshot, pending_call, context)
    print("调用参数:")
    print(json.dumps(pending_call.model_dump(), ensure_ascii=False, indent=2))
    print("Runtime 返回:")
    print(json.dumps(pending.__dict__, ensure_ascii=False, indent=2))

    print("\n========== 4. Runtime execute/finalize：批准原 call id 后执行 ==========")
    approved_context = ExecutionContext(
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        permissions=context.permissions,
        approved_call_ids=frozenset({pending_call.id}),
    )
    success = await runtime.invoke(snapshot, pending_call, approved_context)
    print(f"approved_call_ids: {sorted(approved_context.approved_call_ids)}")
    print("Runtime 返回:")
    print(json.dumps(success.__dict__, ensure_ascii=False, indent=2))
    print("写回 Agent Loop 的结构化输出:")
    print(json.dumps(json.loads(success.content[0]["text"]), ensure_ascii=False, indent=2))

    print("\n========== 5. Runtime 参数校验：非法入参 ==========")
    invalid = await runtime.invoke(
        snapshot,
        ToolCall(
            id="call-invalid-arguments",
            name="create_ticket",
            arguments={"title": "", "description": "x"},
        ),
        approved_context,
    )
    print("Runtime 返回:")
    print(json.dumps(invalid.__dict__, ensure_ascii=False, indent=2))

    print("\n========== 6. Runtime 开关：工具被动态禁用 ==========")
    runtime.enabled["create_ticket"] = False
    disabled = await runtime.invoke(snapshot, pending_call, approved_context)
    print("Runtime 返回:")
    print(json.dumps(disabled.__dict__, ensure_ascii=False, indent=2))

    print("\n========== 7. Audit Log：每次调用的最小审计记录 ==========")
    for index, event in enumerate(audit_log, start=1):
        print(f"审计事件 #{index}:")
        print(json.dumps(event, ensure_ascii=False, indent=2))

    print("\n========== 演示结束 ==========")


if __name__ == "__main__":
    asyncio.run(main())


#                 服务启动 / 插件加载 / 新版本发布
#                                 │
#                                 ▼
# ┌──────────────────────────────────────────────────┐
# │                  ToolRegistry                     │
# │  全局、可变、按 (name, version) 保存工具定义        │
# │                                                    │
# │ create_ticket@v1 ───────────► ToolDefinition       │
# │ create_ticket@v2 ───────────► ToolDefinition       │
# │ search_order@v1   ───────────► ToolDefinition      │
# └──────────────────────────────────────────────────┘
#                                 │
#             某 Agent Run 创建时，应用路由与开关策略
#                                 │
#                                 ▼
# ┌──────────────────────────────────────────────────┐
# │                  ToolSnapshot                     │
# │   单次 Run 专属、只读、冻结的 name → Definition      │
# │                                                    │
# │ create_ticket ───────────────► create_ticket@v1   │
# │ search_order  ───────────────► search_order@v1     │
# └──────────────────────────────────────────────────┘
#                                 │
#                     同一 Run 内稳定使用
#                                 ▼
#                      LLM / ToolRuntime