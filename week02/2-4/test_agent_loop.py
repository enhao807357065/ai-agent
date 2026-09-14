import unittest
from typing import Any

from agent_loop import (
    DemoOrderHost,
    GovernedToolRuntime,
    MinimalAgentLoop,
    ModelReply,
    ToolCall,
    ToolRegistry,
    WaitingForConfirmation,
    build_demo_tools,
)
from tool_models import AuditPhase, AuditSink, ExecutionContext, PermissionEngine


class RefundProvider:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(
                tool_call=ToolCall("refund_001", "order_refund_order", {"order_id": "ord_1001"})
            )
        return ModelReply(text="退款已完成")


class InvalidArgumentsProvider:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(
                tool_call=ToolCall("bad_001", "order_get_order", {"order_id": "bad", "extra": True})
            )
        return ModelReply(text="参数不合法")


class ToolLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.host = DemoOrderHost()
        get_order, refund_order = build_demo_tools(self.host)
        self.registry = ToolRegistry()
        self.registry.register(get_order, "v1")
        self.registry.register(refund_order, "v1")
        self.snapshot = self.registry.snapshot(
            {"order_get_order": "v1", "order_refund_order": "v1"}, "r1"
        )
        self.context = ExecutionContext(
            trace_id="trace_loop_001",
            user_id="user_001",
            tenant_id="tenant_001",
            permissions=frozenset({"order:read", "order:refund"}),
            allow_tools=frozenset(self.snapshot.tools),
        )
        self.audit = AuditSink()
        self.runtime = GovernedToolRuntime(self.registry, PermissionEngine(), self.audit)

    async def test_confirmation_executes_exact_original_call_then_resumes(self) -> None:
        loop = MinimalAgentLoop(RefundProvider(), self.runtime)
        outcome = await loop.run(
            snapshot=self.snapshot, context=self.context, user_message="退款 ord_1001"
        )

        self.assertIsInstance(outcome, WaitingForConfirmation)
        assert isinstance(outcome, WaitingForConfirmation)
        self.assertEqual(self.host.refund_calls, [])
        self.assertEqual(outcome.pending_call.id, "refund_001")
        self.assertEqual(outcome.pending_call.arguments, {"order_id": "ord_1001"})

        answer = await loop.confirm(outcome, approved=True)
        self.assertEqual(answer, "退款已完成")
        self.assertEqual(self.host.refund_calls, ["ord_1001"])
        records = self.audit.by_trace(self.context.trace_id)
        self.assertEqual([(item.phase, item.code) for item in records], [
            (AuditPhase.DECISION, "CONFIRMATION_REQUIRED"),
            (AuditPhase.DECISION, "CONFIRMATION_BYPASSED"),
            (AuditPhase.EXECUTION, "EXECUTED"),
        ])

    async def test_rejection_never_invokes_handler(self) -> None:
        loop = MinimalAgentLoop(RefundProvider(), self.runtime)
        outcome = await loop.run(
            snapshot=self.snapshot, context=self.context, user_message="退款 ord_1001"
        )
        assert isinstance(outcome, WaitingForConfirmation)

        answer = await loop.confirm(outcome, approved=False)
        self.assertEqual(answer, "已取消该操作，未执行工具。")
        self.assertEqual(self.host.refund_calls, [])

    async def test_live_switch_blocks_stale_snapshot_before_handler(self) -> None:
        self.registry.set_family_enabled("order_refund_order", False)
        result = await self.runtime.execute(
            self.snapshot,
            ToolCall("disabled_001", "order_refund_order", {"order_id": "ord_1001"}),
            self.context,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.code, "TOOL_DISABLED")
        self.assertEqual(self.host.refund_calls, [])

    async def test_invalid_arguments_never_reach_handler(self) -> None:
        loop = MinimalAgentLoop(InvalidArgumentsProvider(), self.runtime)
        answer = await loop.run(
            snapshot=self.snapshot, context=self.context, user_message="查询订单"
        )

        self.assertEqual(answer, "参数不合法")
        self.assertEqual(self.host.refund_calls, [])
        record = self.audit.by_trace(self.context.trace_id)[0]
        self.assertEqual(record.code, "INVALID_ARGUMENT")
        self.assertEqual(record.phase, AuditPhase.DECISION)


if __name__ == "__main__":
    unittest.main()
