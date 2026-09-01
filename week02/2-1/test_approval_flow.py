import asyncio
import json
import unittest

from execution_context import ExecutionContext
from mini_agent_loop import reject_order_agent, resume_order_agent
from search_order_tool import CANCEL_ORDER, DemoOrderService
from tool_messages import ToolCall
from tool_runtime import ToolRuntime
from agent_run_state import WaitingForApproval


class FakeMessage:
    tool_calls: list = []
    content = "订单取消流程已完成。"

    def model_dump(self, **_: object) -> dict:
        return {"role": "assistant", "content": self.content}


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = FakeMessage()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


class ApprovalFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_resume_uses_same_call_id_and_executes_handler(self) -> None:
        events: list[dict] = []

        async def trace_writer(event: dict) -> None:
            events.append(event)

        runtime = ToolRuntime(trace_writer=trace_writer)
        runtime.register(CANCEL_ORDER)
        context = ExecutionContext(
            user_id="262789",
            tenant_id="test",
            permission=frozenset({"order:write"}),
            trace_id="trace-approval",
            order_service=DemoOrderService(),
        )
        call = ToolCall(
            id="call-original",
            name="cancel_order",
            arguments_json=json.dumps({"order_id": "ord_1002", "reason": "重复下单"}),
        )
        pending = WaitingForApproval(
            pending_call=call,
            messages=[{"role": "assistant", "tool_calls": []}],
            context=context,
            next_step=2,
            approval_prompt="是否确认？",
        )

        result = await resume_order_agent(pending, runtime, FakeClient())

        self.assertEqual("completed", result.status)
        self.assertEqual("call-original", events[-1]["tool_call_id"])
        self.assertEqual("success", events[-1]["status"])
        self.assertEqual(3, len(pending.messages))
        self.assertEqual("tool", pending.messages[1]["role"])
        self.assertEqual("assistant", pending.messages[2]["role"])

    def test_reject_does_not_execute_handler(self) -> None:
        context = ExecutionContext(
            user_id="262789",
            tenant_id="test",
            permission=frozenset({"order:write"}),
        )
        pending = WaitingForApproval(
            pending_call=ToolCall(id="call-rejected", name="cancel_order", arguments_json="{}"),
            messages=[],
            context=context,
            next_step=2,
            approval_prompt="是否确认？",
        )

        result = reject_order_agent(pending)
        self.assertEqual("completed", result.status)
        self.assertIn("不会修改订单", result.answer)


if __name__ == "__main__":
    unittest.main()
