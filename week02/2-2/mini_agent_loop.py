"""week02 / 2-2：带完整 ToolRuntime 调用链的 mini agent loop（v3）。

本文件刻意保持为单文件教学示例，方便顺着一次 Agent Run 阅读：

Agent Run 创建
  -> ToolRegistry.snapshot()：按 route + 开关选择并冻结工具版本
  -> 模型（此处由 FakePlanner 模拟）产出不可信 ToolCall
  -> ToolRuntime.invoke(snapshot, call, ctx)
     -> Snapshot 查找 -> 实时开关 -> 参数校验 -> 权限 -> handler
     -> 输出校验 -> 脱敏审计 -> ToolResultMessage
  -> Agent Loop 将 ToolResultMessage 写回消息历史

真实系统中，Registry/Runtime/Contracts/Handlers 通常应拆分成独立模块；这里合并只是为了学习。
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


# ---------------------------------------------------------------------------
# 1. Contract：LLM 传入的是不可信 ToolCall；工具入/出参都必须严格校验。
# ---------------------------------------------------------------------------
class StrictModel(BaseModel):
    """拒绝未声明字段，避免 LLM/调用方通过额外字段进行参数走私。"""

    model_config = ConfigDict(extra="forbid")


class GetWeatherInput(StrictModel):
    city: str = Field(min_length=1, max_length=30, description="要查询天气的城市")


class GetWeatherOutput(StrictModel):
    city: str
    temperature_c: float
    condition: str


class CreateOrderInput(StrictModel):
    product_id: str = Field(pattern=r"^sku_[a-z0-9_]+$")
    quantity: int = Field(ge=1, le=10)
    # 业务幂等键：写工具若要支持安全重试，必须由下游据此去重。
    idempotency_key: str = Field(min_length=8, max_length=64)


class CreateOrderOutput(StrictModel):
    order_id: str
    status: Literal["created"]
    product_id: str
    quantity: int


class ToolCall(StrictModel):
    """模型返回的调用请求。版本不能由模型指定，版本来自服务端 Snapshot。"""

    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultMessage(StrictModel):
    """Runtime 交还 Agent Loop 的唯一结果契约。"""

    tool_call_id: str
    tool_name: str
    success: bool
    content: str
    details: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None

    def to_model_message(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions 可追加的 tool 消息形状。"""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": self.content,
        }


@dataclass(frozen=True)
class ExecutionContext:
    """可信的服务端上下文，绝不允许模型经 ToolCall 伪造 user/permission。"""

    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    trace_id: str
    # 高风险操作必须绑定到精确的 ToolCall ID，不能仅按工具名全局放行。
    approved_call_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RetryPolicy:
    """max_retries 是“额外重试次数”，因此总尝试次数 = max_retries + 1。"""

    max_retries: int = 0
    retry_on_timeout: bool = False


ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    """工具的声明式契约 + 可执行 handler。

    permission=None 表示：已经认证的请求不需额外业务权限，而非匿名开放。
    is_idempotent 决定 Runtime 是否允许超时后重试。
    risk 是影响等级，不替代权限：low 通常是低影响读取，medium 仅加强治理；
    high 默认要求针对本次 ToolCall 的用户确认。
    """

    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    permission: str | None
    risk: Literal["low", "medium", "high"]
    is_idempotent: bool
    retry_policy: RetryPolicy
    timeout_seconds: float
    audit_fields: tuple[str, ...]

    def to_provider_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


@dataclass(frozen=True)
class ToolSnapshot:
    """某一次 Agent Run 的不可变工具视图：name -> 服务端选定的 Definition。"""

    tools: Mapping[str, ToolDefinition]
    route_revision: str

    def provider_tools(self) -> list[dict[str, Any]]:
        return [tool.to_provider_tool() for tool in self.tools.values()]


# ---------------------------------------------------------------------------
# 2. Registry：全局可变的多版本工具仓库，生成每次 Run 不可变 Snapshot。
# ---------------------------------------------------------------------------
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDefinition] = {}
        # 精确到版本的实时开关：可杀掉 v1，同时保留 v2。
        self._version_enabled: dict[tuple[str, str], bool] = {}
        # 工具家族级 kill switch：紧急时禁用某工具的所有版本。
        self._family_enabled: dict[str, bool] = {}

    def register(self, tool: ToolDefinition) -> None:
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(f"重复注册工具：{tool.name}@{tool.version}")
        if tool.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if tool.retry_policy.max_retries < 0:
            raise ValueError("max_retries 不可小于 0")
        self._tools[key] = tool
        self._version_enabled.setdefault(key, True)
        self._family_enabled.setdefault(tool.name, True)

    def set_version_enabled(self, name: str, version: str, enabled: bool) -> None:
        if (name, version) not in self._tools:
            raise KeyError(f"工具未注册：{name}@{version}")
        self._version_enabled[(name, version)] = enabled

    def set_family_enabled(self, name: str, enabled: bool) -> None:
        self._family_enabled[name] = enabled

    def is_live_enabled(self, tool: ToolDefinition) -> bool:
        """Snapshot 创建和 invoke 都调用此方法；后者用于防止运行时漂移。"""
        key = (tool.name, tool.version)
        return self._family_enabled.get(tool.name, False) and self._version_enabled.get(key, False)

    def snapshot(self, routes: Mapping[str, str], route_revision: str) -> ToolSnapshot:
        selected: dict[str, ToolDefinition] = {}
        for name, version in routes.items():
            tool = self._tools.get((name, version))
            if tool is None:
                raise KeyError(f"路由到未注册工具：{name}@{version}")
            # 新 Run 不应暴露已被禁用的工具。
            if self.is_live_enabled(tool):
                selected[name] = tool
        return ToolSnapshot(tools=MappingProxyType(selected), route_revision=route_revision)


# ---------------------------------------------------------------------------
# 3. Runtime：统一处理不可信输入、实时治理、重试、审计和结果封装。
# ---------------------------------------------------------------------------
class ToolRuntime:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self.audit_events: list[dict[str, Any]] = []

    def _audit(
        self,
        call: ToolCall,
        ctx: ExecutionContext,
        tool: ToolDefinition | None,
        *,
        outcome: str,
        attempt: int,
        latency_ms: int,
        error_code: str | None = None,
    ) -> None:
        """只记录 allowlist 中字段的“是否提供”，不记录值，模拟脱敏审计。"""
        self.audit_events.append(
            {
                "trace_id": ctx.trace_id,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "tool_version": tool.version if tool else None,
                "risk": tool.risk if tool else None,
                "user_id": ctx.user_id,
                "tenant_id": ctx.tenant_id,
                "outcome": outcome,
                "error_code": error_code,
                "attempt": attempt,
                "latency_ms": latency_ms,
                "redacted_argument_fields": sorted(
                    key for key in call.arguments if tool and key in tool.audit_fields
                ),
            }
        )

    def _failure(
        self,
        call: ToolCall,
        ctx: ExecutionContext,
        tool: ToolDefinition | None,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempt: int = 0,
        started_at: float | None = None,
    ) -> ToolResultMessage:
        latency_ms = int((time.perf_counter() - started_at) * 1000) if started_at else 0
        self._audit(
            call, ctx, tool, outcome="error", error_code=code,
            attempt=attempt, latency_ms=latency_ms,
        )
        return ToolResultMessage(
            tool_call_id=call.id,
            tool_name=call.name,
            success=False,
            content=json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False),
            details={
                "version": tool.version if tool else None,
                "risk": tool.risk if tool else None,
                "attempt": attempt,
            },
            error={"code": code, "message": message, "retryable": retryable},
        )

    async def invoke(self, snapshot: ToolSnapshot, call: ToolCall, ctx: ExecutionContext) -> ToolResultMessage:
        """完整受控调用链。仅在所有前置检查通过后才运行 handler。"""
        started_at = time.perf_counter()
        tool = snapshot.tools.get(call.name)
        if tool is None:
            return self._failure(call, ctx, None, "TOOL_NOT_FOUND", "本次 run 的快照中不存在该工具")

        # Snapshot 固定“本次调用哪个版本”；实时开关固定“此刻是否仍准许执行”。
        if not self._registry.is_live_enabled(tool):
            return self._failure(call, ctx, tool, "TOOL_DISABLED", "工具当前已停止")

        try:
            params = tool.input_model.model_validate(call.arguments)
        except ValidationError:
            return self._failure(call, ctx, tool, "INVALID_ARGUMENT", "工具参数不符合 Schema")

        if tool.permission is not None and tool.permission not in ctx.permissions:
            return self._failure(call, ctx, tool, "PERMISSION_DENIED", "当前身份没有调用该工具的权限")

        # permission 表示“有资格”；risk=high 表示“此操作仍需用户批准”。
        # 批准绑定 call.id，避免用户同意订单 A，却被复用去创建订单 B。
        if tool.risk == "high" and call.id not in ctx.approved_call_ids:
            return self._failure(
                call, ctx, tool, "APPROVAL_REQUIRED",
                "高风险工具需要用户确认后才能执行",
            )

        total_attempts = tool.retry_policy.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                raw_output = await asyncio.wait_for(
                    tool.handler(params, ctx), timeout=tool.timeout_seconds
                )
                break
            except TimeoutError:
                retry_allowed = (
                    tool.is_idempotent
                    and tool.retry_policy.retry_on_timeout
                    and attempt < total_attempts
                )
                if retry_allowed:
                    continue
                return self._failure(
                    call, ctx, tool, "TIMEOUT", "工具执行超时",
                    retryable=tool.is_idempotent and tool.retry_policy.retry_on_timeout,
                    attempt=attempt, started_at=started_at,
                )
            except Exception:
                # 不将内部异常/堆栈泄露给模型；生产中需额外记录内部诊断日志。
                return self._failure(
                    call, ctx, tool, "HANDLER_ERROR", "工具执行失败",
                    attempt=attempt, started_at=started_at,
                )
        else:  # 理论上不会到达，保留是为了让控制流完整。
            return self._failure(call, ctx, tool, "HANDLER_ERROR", "工具未产生结果")

        try:
            output = tool.output_model.model_validate(raw_output)
        except ValidationError:
            return self._failure(
                call, ctx, tool, "INVALID_OUTPUT", "工具返回值不符合输出契约",
                attempt=attempt, started_at=started_at,
            )

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        self._audit(call, ctx, tool, outcome="success", attempt=attempt, latency_ms=latency_ms)
        return ToolResultMessage(
            tool_call_id=call.id,
            tool_name=tool.name,
            success=True,
            content=json.dumps(output.model_dump(), ensure_ascii=False, sort_keys=True),
            details={
                "version": tool.version,
                "risk": tool.risk,
                "attempt": attempt,
                "latency_ms": latency_ms,
            },
        )

    async def invoke_success_or_failure(
        self, snapshot: ToolSnapshot, call: ToolCall, ctx: ExecutionContext
    ) -> ToolResultMessage:
        """Agent Loop 的便捷封装：invoke 自身已经统一将可预期失败封装为 Result。"""
        return await self.invoke(snapshot, call, ctx)


# ---------------------------------------------------------------------------
# 4. 两个业务 Handler：只接受已验证 params + 可信 ctx，不负责通用治理。
# ---------------------------------------------------------------------------
async def get_weather_handler(params: BaseModel, _: ExecutionContext) -> dict[str, Any]:
    args = GetWeatherInput.model_validate(params)
    await asyncio.sleep(0.01)  # 模拟 HTTP 请求
    weather = {"北京": (18.5, "晴"), "上海": (21.0, "多云")}
    temperature_c, condition = weather.get(args.city, (20.0, "阴"))
    return {"city": args.city, "temperature_c": temperature_c, "condition": condition}


class DemoOrderStore:
    """模拟下游订单服务：以 idempotency_key 保证写操作可安全重试。"""

    def __init__(self) -> None:
        self.orders_by_key: dict[str, dict[str, Any]] = {}

    async def create(self, args: CreateOrderInput, user_id: str) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        if args.idempotency_key not in self.orders_by_key:
            self.orders_by_key[args.idempotency_key] = {
                "order_id": f"ord_{uuid4().hex[:8]}",
                "status": "created",
                "product_id": args.product_id,
                "quantity": args.quantity,
                # 实际 DB 不应把 user_id 直接回传给 LLM；此处也不输出。
                "owner": user_id,
            }
        result = self.orders_by_key[args.idempotency_key]
        return {key: result[key] for key in ("order_id", "status", "product_id", "quantity")}


def build_create_order_handler(store: DemoOrderStore) -> ToolHandler:
    async def create_order_handler(params: BaseModel, ctx: ExecutionContext) -> dict[str, Any]:
        args = CreateOrderInput.model_validate(params)
        return await store.create(args, ctx.user_id)

    return create_order_handler


# ---------------------------------------------------------------------------
# 5. Mini Agent Loop：FakePlanner 代替真实 LLM，重点展示 Runtime 结果回写。
# ---------------------------------------------------------------------------
class FakePlanner:
    def plan(self, user_text: str) -> list[ToolCall]:
        """真实项目中由 LLM SDK 的 tool_calls 替换；其输出仍要按不可信输入处理。"""
        if "天气" in user_text:
            return [ToolCall(id="call_weather_001", name="get_weather", arguments={"city": "北京"})]
        return [
            ToolCall(
                id="call_order_001",
                name="create_order",
                arguments={"product_id": "sku_python_book", "quantity": 1, "idempotency_key": "demo-order-001"},
            )
        ]


async def run_agent(
    user_text: str, runtime: ToolRuntime, snapshot: ToolSnapshot, ctx: ExecutionContext
) -> list[dict[str, Any]]:
    """一次极简 Agent Run：创建快照已在调用前完成，循环只执行快照中的工具。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "你是一个工具调用助手。"},
        {"role": "user", "content": user_text},
    ]
    for call in FakePlanner().plan(user_text):
        messages.append({"role": "assistant", "tool_calls": [call.model_dump()]})
        result = await runtime.invoke_success_or_failure(snapshot, call, ctx)
        messages.append(result.to_model_message())
        print(f"\nToolCall: {call.model_dump()}")
        print(f"ToolResultMessage: {result.model_dump()}")
    return messages


async def main() -> None:
    store = DemoOrderStore()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="get_weather", version="v1", description="查询指定城市的当前天气。",
            input_model=GetWeatherInput, output_model=GetWeatherOutput,
            handler=get_weather_handler, permission="weather:read", risk="low", is_idempotent=True,
            retry_policy=RetryPolicy(max_retries=1, retry_on_timeout=True),
            timeout_seconds=1.0, audit_fields=("city",),
        )
    )
    registry.register(
        ToolDefinition(
            name="get_weather", version="v2", description="查询指定城市的当前天气（v2）。",
            input_model=GetWeatherInput, output_model=GetWeatherOutput,
            handler=get_weather_handler, permission="weather:read", risk="low", is_idempotent=True,
            retry_policy=RetryPolicy(max_retries=1, retry_on_timeout=True),
            timeout_seconds=1.0, audit_fields=("city",),
        )
    )
    registry.register(
        ToolDefinition(
            name="create_order", version="v1", description="创建订单；必须提供业务幂等键。",
            input_model=CreateOrderInput, output_model=CreateOrderOutput,
            handler=build_create_order_handler(store), permission="order:write", risk="high", is_idempotent=True,
            retry_policy=RetryPolicy(max_retries=1, retry_on_timeout=True),
            timeout_seconds=1.0, audit_fields=("product_id", "quantity", "idempotency_key"),
        )
    )

    # === Agent Run 创建期：Snapshot 检查当前路由版本及开关，并冻结工具选择 ===
    snapshot_v1 = registry.snapshot(
        routes={"get_weather": "v1", "create_order": "v1"}, route_revision="route-r1"
    )
    print("========== 1. Agent Run Snapshot / Provider Tools ==========")
    print(json.dumps(snapshot_v1.provider_tools(), ensure_ascii=False, indent=2))
    print("本次 run 中 get_weather 版本：", snapshot_v1.tools["get_weather"].version)

    # 新 Run 可以路由 v2；旧 Snapshot 依然引用 v1，验证不可变的版本选择。
    snapshot_v2 = registry.snapshot(
        routes={"get_weather": "v2", "create_order": "v1"}, route_revision="route-r2"
    )
    print("新 run 路由 get_weather@v2，旧 snapshot 仍为：", snapshot_v1.tools["get_weather"].version)
    print("新 snapshot 为：", snapshot_v2.tools["get_weather"].version)

    runtime = ToolRuntime(registry)
    ctx = ExecutionContext(
        user_id="user_100", tenant_id="tenant_demo",
        permissions=frozenset({"weather:read", "order:write"}), trace_id="trace-demo-001",
    )

    print("\n========== 2. 正常 Agent Loop：天气工具 ==========")
    await run_agent("帮我查询北京天气", runtime, snapshot_v1, ctx)

    print("\n========== 3. 高风险 create_order：先返回审批中 ==========")
    pending_order_call = ToolCall(
        id="call_order_001",
        name="create_order",
        arguments={"product_id": "sku_python_book", "quantity": 1, "idempotency_key": "demo-order-001"},
    )
    approval_required = await runtime.invoke(snapshot_v1, pending_order_call, ctx)
    print(approval_required.model_dump())

    # 模拟 UI 已展示订单参数，用户明确确认后，必须重用原始 call.id 和原始参数。
    approved_ctx = replace(
        ctx,
        approved_call_ids=ctx.approved_call_ids | frozenset({pending_order_call.id}),
    )
    approved_order = await runtime.invoke(snapshot_v1, pending_order_call, approved_ctx)
    print("确认原始 ToolCall 后执行：", approved_order.model_dump())

    # 关键测试点：Snapshot 固定 v1；执行前实时检查版本开关，所以 v1 被停止后旧 Run 也不能继续执行。
    print("\n========== 4. 运行时工具漂移防护：停止 get_weather@v1 ==========")
    registry.set_version_enabled("get_weather", "v1", False)
    disabled = await runtime.invoke(
        snapshot_v1,
        ToolCall(id="call_disabled_001", name="get_weather", arguments={"city": "上海"}),
        ctx,
    )
    print(disabled.model_dump())
    print("v2 未被停止，新 snapshot 仍可用：", registry.is_live_enabled(snapshot_v2.tools["get_weather"]))

    print("\n========== 5. 参数校验：多余字段被 StrictModel 拒绝 ==========")
    invalid = await runtime.invoke(
        snapshot_v2,
        ToolCall(id="call_invalid_001", name="get_weather", arguments={"city": "上海", "admin": True}),
        ctx,
    )
    print(invalid.model_dump())

    print("\n========== 6. 权限校验：没有 order:write 不会触发 handler ==========")
    readonly_ctx = ExecutionContext(
        user_id="user_readonly", tenant_id="tenant_demo",
        permissions=frozenset({"weather:read"}), trace_id="trace-demo-002",
    )
    denied = await runtime.invoke(
        snapshot_v2,
        ToolCall(
            id="call_denied_001", name="create_order",
            arguments={"product_id": "sku_python_book", "quantity": 1, "idempotency_key": "denied-001"},
        ),
        readonly_ctx,
    )
    print(denied.model_dump())

    print("\n========== 7. 最小化脱敏审计日志 ==========")
    for event in runtime.audit_events:
        print(json.dumps(event, ensure_ascii=False))

    # 聚焦回归断言：教学 main 同时打印现象和断言关键策略。
    assert snapshot_v1.tools["get_weather"].version == "v1"
    assert snapshot_v2.tools["get_weather"].version == "v2"
    assert disabled.error and disabled.error["code"] == "TOOL_DISABLED"
    assert invalid.error and invalid.error["code"] == "INVALID_ARGUMENT"
    assert denied.error and denied.error["code"] == "PERMISSION_DENIED"
    assert approval_required.error and approval_required.error["code"] == "APPROVAL_REQUIRED"
    assert approved_order.success is True
    assert approved_order.details["risk"] == "high"
    assert all("argument_values" not in event for event in runtime.audit_events)
    print("\n✅ 关键测试点通过：快照版本、实时停用、参数校验、权限校验、高风险审批、脱敏审计。")


if __name__ == "__main__":
    asyncio.run(main())
