import asyncio
import json
import unittest

from pydantic import BaseModel, ConfigDict

from execution_context import ExecutionContext
from tool_contracts import ToolError, ToolErrorCode
from tool_definition import ToolDefinition
from tool_messages import ToolCall
from tool_runtime import ToolRuntime


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class CustomToolError(ToolError):
    """证明 Runtime 使用 ToolDefinition 声明的错误模型。"""

    pass


async def echo_handler(args: EchoInput, ctx: ExecutionContext) -> EchoOutput:
    return EchoOutput(text=args.text)


class ToolRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.events: list[dict] = []

        async def trace_writer(event: dict) -> None:
            self.events.append(event)

        self.runtime = ToolRuntime(trace_writer=trace_writer)
        self.ctx = ExecutionContext(
            user_id="user-1",
            tenant_id="tenant-1",
            permission=frozenset(),
            trace_id="trace-1",
        )

    async def test_public_tool_is_visible_and_executable(self) -> None:
        self.runtime.register(ToolDefinition(
            name="echo",
            description="返回输入文本",
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            permission=None,
            audit_log=True,
        ))

        self.assertEqual(["echo"], [item["function"]["name"] for item in self.runtime.model_tools(self.ctx)])
        result = await self.runtime.execute(
            ToolCall(id="call-1", name="echo", arguments_json='{"text": "hello"}'),
            self.ctx,
        )

        self.assertFalse(result.is_error)
        self.assertEqual({"text": "hello"}, json.loads(result.content))
        self.assertEqual("success", self.events[-1]["status"])
        self.assertEqual("v1", self.events[-1]["tool_version"])

    async def test_confirmation_is_bound_to_this_call_id(self) -> None:
        self.runtime.register(ToolDefinition(
            name="confirm_echo",
            description="需要确认的工具",
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            requires_confirmation=True,
        ))
        call = ToolCall(id="call-to-confirm", name="confirm_echo", arguments_json='{"text": "ok"}')

        blocked = await self.runtime.execute(call, self.ctx)
        self.assertTrue(blocked.is_error)
        self.assertEqual(
            ToolErrorCode.APPROVAL_REQUIRED,
            json.loads(blocked.content)["error"]["code"],
        )

        approved_ctx = ExecutionContext(
            user_id=self.ctx.user_id,
            tenant_id=self.ctx.tenant_id,
            permission=self.ctx.permission,
            approved_call_ids=frozenset({call.id}),
            trace_id=self.ctx.trace_id,
        )
        allowed = await self.runtime.execute(call, approved_ctx)
        self.assertFalse(allowed.is_error)

    async def test_declared_error_model_and_audit_switch(self) -> None:
        self.runtime.register(ToolDefinition(
            name="custom_error",
            description="输入校验错误使用自定义模型",
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            error_model=CustomToolError,
            audit_log=False,
        ))

        result = await self.runtime.execute(
            ToolCall(id="invalid-call", name="custom_error", arguments_json="{}"),
            self.ctx,
        )

        error = json.loads(result.content)["error"]
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENT, error["code"])
        self.assertEqual("trace-1", error["trace_id"])
        self.assertEqual([], self.events)


if __name__ == "__main__":
    unittest.main()
