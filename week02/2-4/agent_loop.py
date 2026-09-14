"""2-4：把 ToolRegistry、ToolRuntime、PermissionEngine 串起来的最小 Agent Loop。

运行：
    conda run -n ai-agent python agent_loop.py

本例刻意不用真实 LLM / MCP 进程，而是使用 ScriptedProvider 和一个 Host 包装后的
业务工具，确保学习重点落在受控调用链：

    Agent Host 注册服务端 ToolDefinition
      -> ToolRegistry 为本次 Run 创建不可变 Snapshot
      -> Provider 只看到 Snapshot 导出的 JSON Schema
      -> 模型 ToolCall（不可信）
      -> Runtime: Snapshot 查找 -> live switch -> Pydantic -> PermissionEngine
      -> handler（这里可替换为 MCP client.call_tool 的适配器）
      -> Agent Loop 回写 tool result，或在需要确认时停止并保存原始调用

MCP Host 的关键边界：模型只认识 Host 的 public tool name；server_id、remote_name、
transport 和连接凭据都应封装在 handler 的适配器内，不能由模型参数决定。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import Field, ValidationError

from tool_models import (
    AuditPhase,
    AuditRecord,
    AuditSink,
    ExecutionContext,
    ExecutionMode,
    PermissionDecision,
    PermissionEngine,
    PermissionResult,
    RiskLevel,
    ToolArguments,
    ToolDefinition,
    ToolEffect,
    ToolPolicy,
)


MAX_MODEL_RESULT_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型产生的不可信调用。arguments 在保存 pending state 时冻结，避免确认前被篡改。"""

    id: str
    name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str | None = None
    tool_call: ToolCall | None = None


class AgentProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """Host 内部的版本化注册项；版本永远不暴露给模型。"""

    tool: ToolDefinition
    version: str


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    """一次 Run 固定的 name -> 服务端选定工具版本。"""

    tools: Mapping[str, RegisteredTool]
    route_revision: str

    def model_tools(self) -> list[dict[str, Any]]:
        return [registered.tool.to_model_tool() for registered in self.tools.values()]


class ToolRegistry:
    """Registry 是全局可变控制面；Snapshot 是单次 Run 的不可变数据面。"""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}
        self._family_enabled: dict[str, bool] = {}
        self._version_enabled: dict[tuple[str, str], bool] = {}

    def register(self, tool: ToolDefinition, version: str) -> None:
        key = (tool.name, version)
        if key in self._tools:
            raise ValueError(f"重复注册工具：{tool.name}@{version}")
        self._tools[key] = RegisteredTool(tool=tool, version=version)
        self._family_enabled.setdefault(tool.name, True)
        self._version_enabled.setdefault(key, True)

    def set_family_enabled(self, name: str, enabled: bool) -> None:
        self._family_enabled[name] = enabled

    def set_version_enabled(self, name: str, version: str, enabled: bool) -> None:
        key = (name, version)
        if key not in self._tools:
            raise KeyError(f"工具未注册：{name}@{version}")
        self._version_enabled[key] = enabled

    def is_live_enabled(self, registered: RegisteredTool) -> bool:
        key = (registered.tool.name, registered.version)
        return self._family_enabled.get(registered.tool.name, False) and self._version_enabled.get(key, False)

    def snapshot(self, routes: Mapping[str, str], route_revision: str) -> ToolSnapshot:
        selected: dict[str, RegisteredTool] = {}
        for name, version in routes.items():
            registered = self._tools.get((name, version))
            if registered is None:
                raise KeyError(f"路由到未注册工具：{name}@{version}")
            # 新 Run 不向模型暴露已禁用工具。
            if self.is_live_enabled(registered):
                selected[name] = registered
        return ToolSnapshot(MappingProxyType(selected), route_revision)


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Runtime 的统一返回，不把 handler 异常或原始参数泄露回模型。"""

    tool_call_id: str
    tool_name: str
    permission: PermissionResult | None
    success: bool
    code: str
    content: str

    def to_model_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": self.content,
        }


class GovernedToolRuntime:
    """执行安全边界：实时开关、schema、权限、handler 和最小审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        permission_engine: PermissionEngine,
        audit_sink: AuditSink,
    ) -> None:
        self._registry = registry
        self._permission_engine = permission_engine
        self._audit_sink = audit_sink

    def _audit(
        self,
        *,
        call: ToolCall,
        context: ExecutionContext,
        phase: AuditPhase,
        decision: PermissionDecision,
        code: str,
        latency_ms: int | None = None,
    ) -> None:
        self._audit_sink.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase=phase,
                decision=decision,
                code=code,
                argument_keys=frozenset(call.arguments),
                latency_ms=latency_ms,
            )
        )

    async def execute(
        self,
        snapshot: ToolSnapshot,
        call: ToolCall,
        context: ExecutionContext,
    ) -> RuntimeResult:
        registered = snapshot.tools.get(call.name)
        if registered is None:
            self._audit(
                call=call, context=context, phase=AuditPhase.DECISION,
                decision=PermissionDecision.DENY, code="TOOL_NOT_FOUND",
            )
            return RuntimeResult(call.id, call.name, None, False, "TOOL_NOT_FOUND", self._error_content("TOOL_NOT_FOUND"))

        # Snapshot 可来自较早的模型上下文；执行前必须重查全局实时开关。
        if not self._registry.is_live_enabled(registered):
            self._audit(
                call=call, context=context, phase=AuditPhase.DECISION,
                decision=PermissionDecision.DENY, code="TOOL_DISABLED",
            )
            return RuntimeResult(call.id, call.name, None, False, "TOOL_DISABLED", self._error_content("TOOL_DISABLED"))

        tool = registered.tool
        try:
            arguments = tool.parameters_model.model_validate(call.arguments)
        except ValidationError:
            self._audit(
                call=call, context=context, phase=AuditPhase.DECISION,
                decision=PermissionDecision.DENY, code="INVALID_ARGUMENT",
            )
            return RuntimeResult(call.id, call.name, None, False, "INVALID_ARGUMENT", self._error_content("INVALID_ARGUMENT"))

        permission = await self._permission_engine.decide(tool, arguments, context)
        self._audit(
            call=call, context=context, phase=AuditPhase.DECISION,
            decision=permission.decision, code=permission.reason_code,
        )
        if permission.decision is not PermissionDecision.ALLOW:
            return RuntimeResult(
                call.id, call.name, permission, False, permission.reason_code,
                self._error_content(permission.reason_code),
            )

        started_at = time.perf_counter()
        try:
            # ToolDefinition.handler 是 Host 的适配边界：真实场景中此处可调用 MCP client.call_tool。
            raw_output = await tool.handler(arguments, context)
            if not isinstance(raw_output, Mapping):
                raise TypeError("handler must return Mapping")
            content = json.dumps(dict(raw_output), ensure_ascii=False, sort_keys=True)
            if len(content) > MAX_MODEL_RESULT_CHARS:
                raise ValueError("tool result exceeds model context budget")
        except Exception:
            latency_ms = int((time.perf_counter() - started_at) * 1_000)
            self._audit(
                call=call, context=context, phase=AuditPhase.EXECUTION,
                decision=PermissionDecision.DENY, code="HANDLER_FAILED", latency_ms=latency_ms,
            )
            return RuntimeResult(call.id, call.name, permission, False, "HANDLER_FAILED", self._error_content("HANDLER_FAILED"))

        latency_ms = int((time.perf_counter() - started_at) * 1_000)
        self._audit(
            call=call, context=context, phase=AuditPhase.EXECUTION,
            decision=PermissionDecision.ALLOW, code="EXECUTED", latency_ms=latency_ms,
        )
        return RuntimeResult(call.id, call.name, permission, True, "EXECUTED", content)

    @staticmethod
    def _error_content(code: str) -> str:
        return json.dumps({"error": {"code": code}}, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class WaitingForConfirmation:
    """Agent 中断状态：必须保存原始 call，恢复时禁止重新让模型生成调用。"""

    pending_call: ToolCall
    snapshot: ToolSnapshot
    messages: list[dict[str, Any]]
    context: ExecutionContext
    next_step: int
    prompt: str


class MinimalAgentLoop:
    def __init__(self, provider: AgentProvider, runtime: GovernedToolRuntime) -> None:
        self._provider = provider
        self._runtime = runtime

    async def run(
        self,
        *,
        snapshot: ToolSnapshot,
        context: ExecutionContext,
        user_message: str,
        max_steps: int = 3,
    ) -> str | WaitingForConfirmation:
        messages = [{"role": "user", "content": user_message}]
        return await self._continue(snapshot, context, messages, 1, max_steps)

    async def confirm(self, pending: WaitingForConfirmation, approved: bool, max_steps: int = 3) -> str | WaitingForConfirmation:
        if not approved:
            return "已取消该操作，未执行工具。"

        # 只更新可信交互档位，并执行用户刚刚确认的 immutable pending_call。
        # 不重新请求模型，因此不会发生“确认订单 A，实际执行订单 B”。
        approved_context = replace(pending.context, mode=ExecutionMode.BYPASS_PERMISSIONS)
        result = await self._runtime.execute(pending.snapshot, pending.pending_call, approved_context)
        if not result.success:
            return f"确认后的工具调用未执行：{result.code}"

        messages = [*pending.messages, result.to_model_message()]
        return await self._continue(
            pending.snapshot,
            approved_context,
            messages,
            pending.next_step,
            max_steps,
        )

    async def _continue(
        self,
        snapshot: ToolSnapshot,
        context: ExecutionContext,
        messages: list[dict[str, Any]],
        start_step: int,
        max_steps: int,
    ) -> str | WaitingForConfirmation:
        model_tools = snapshot.model_tools()
        for step in range(start_step, max_steps + 1):
            reply = await self._provider.complete(messages, model_tools)
            if reply.tool_call is None:
                if reply.text is None:
                    raise RuntimeError("MODEL_REPLY_INVALID")
                return reply.text

            call = reply.tool_call
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": [{
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                        },
                    }],
                }
            )
            result = await self._runtime.execute(snapshot, call, context)

            if result.permission is not None and result.permission.decision is PermissionDecision.CONFIRM:
                return WaitingForConfirmation(
                    pending_call=call,
                    snapshot=snapshot,
                    messages=messages,
                    context=context,
                    next_step=step + 1,
                    prompt="该操作会产生副作用，是否确认执行？",
                )
            if result.permission is not None and result.permission.decision is PermissionDecision.APPROVAL_REQUIRED:
                # 本练习将强制审批和普通确认都建模为安全中断；生产版应使用独立审批记录。
                return WaitingForConfirmation(
                    pending_call=call,
                    snapshot=snapshot,
                    messages=messages,
                    context=context,
                    next_step=step + 1,
                    prompt="该高风险操作需要审批，是否确认？",
                )

            messages.append(result.to_model_message())

        raise RuntimeError("MAX_STEPS_EXCEEDED")


# ---------------------------------------------------------------------------
# 以下是可运行 Demo：Host 将内部 MCP 服务封装为两个窄业务工具。
# ---------------------------------------------------------------------------
class GetOrderArguments(ToolArguments):
    order_id: str = Field(pattern=r"^ord_[0-9]+$")


class RefundOrderArguments(ToolArguments):
    order_id: str = Field(pattern=r"^ord_[0-9]+$")


class DemoOrderHost:
    """模拟 MCP Host adapter。真实版本由 handler 内部调用固定 server_id / remote_name。"""

    def __init__(self) -> None:
        self.refund_calls: list[str] = []

    async def get_order(self, arguments: ToolArguments, context: ExecutionContext) -> Mapping[str, Any]:
        order_id = GetOrderArguments.model_validate(arguments).order_id
        return {"order_id": order_id, "status": "paid", "tenant": context.tenant_id}

    async def refund_order(self, arguments: ToolArguments, context: ExecutionContext) -> Mapping[str, Any]:
        order_id = RefundOrderArguments.model_validate(arguments).order_id
        self.refund_calls.append(order_id)
        return {"order_id": order_id, "status": "refunded"}


def build_demo_tools(host: DemoOrderHost) -> tuple[ToolDefinition, ToolDefinition]:
    return (
        ToolDefinition(
            name="order_get_order",
            description="查询当前租户内订单状态。",
            parameters_model=GetOrderArguments,
            tool_policy=ToolPolicy(ToolEffect.READ, RiskLevel.LOW, frozenset({"order:read"})),
            handler=host.get_order,
            precheck=None,
            canonical_target=lambda args, ctx: f"{ctx.tenant_id}:{GetOrderArguments.model_validate(args).order_id}",
        ),
        ToolDefinition(
            name="order_refund_order",
            description="为当前租户的订单发起退款；执行前必须用户确认。",
            parameters_model=RefundOrderArguments,
            tool_policy=ToolPolicy(
                ToolEffect.WRITE,
                RiskLevel.HIGH,
                frozenset({"order:refund"}),
                requires_confirmation=True,
            ),
            handler=host.refund_order,
            precheck=None,
            canonical_target=lambda args, ctx: f"{ctx.tenant_id}:{RefundOrderArguments.model_validate(args).order_id}",
        ),
    )


class DemoProvider:
    """确定性 Provider：先请求退款，收到真实 tool result 后再生成最终回答。"""

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        if not any(message["role"] == "tool" for message in messages):
            name = next(tool["function"]["name"] for tool in tools if tool["function"]["name"] == "order_refund_order")
            return ModelReply(tool_call=ToolCall("demo_refund_001", name, {"order_id": "ord_1001"}))
        result = json.loads(messages[-1]["content"])
        return ModelReply(text=f"订单 {result['order_id']} 已退款，当前状态：{result['status']}。")


async def main() -> None:
    host = DemoOrderHost()
    get_order, refund_order = build_demo_tools(host)
    registry = ToolRegistry()
    registry.register(get_order, "v1")
    registry.register(refund_order, "v1")
    snapshot = registry.snapshot(
        {"order_get_order": "v1", "order_refund_order": "v1"},
        route_revision="demo-r1",
    )
    context = ExecutionContext(
        trace_id="trace_demo_001",
        user_id="user_001",
        tenant_id="tenant_001",
        permissions=frozenset({"order:read", "order:refund"}),
        allow_tools=frozenset(snapshot.tools),
    )
    audit = AuditSink()
    runtime = GovernedToolRuntime(registry, PermissionEngine(), audit)
    loop = MinimalAgentLoop(DemoProvider(), runtime)

    print("MODEL TOOLS:", [item["function"]["name"] for item in snapshot.model_tools()])
    outcome = await loop.run(snapshot=snapshot, context=context, user_message="退款订单 ord_1001")
    assert isinstance(outcome, WaitingForConfirmation)
    print("WAITING:", outcome.prompt, "pending_call=", outcome.pending_call.id)
    answer = await loop.confirm(outcome, approved=True)
    print("ANSWER:", answer)
    print("HOST REFUND CALLS:", host.refund_calls)
    print("AUDIT:")
    for record in audit.by_trace(context.trace_id):
        print(record)


if __name__ == "__main__":
    asyncio.run(main())
