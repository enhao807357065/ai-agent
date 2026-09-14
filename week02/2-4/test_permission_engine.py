import unittest
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from tool_models import (
    ApprovalVerifier,
    ExecutionContext,
    ExecutionMode,
    PermissionDecision,
    PermissionEngine,
    PermissionStage,
    PrecheckDenied,
    RiskLevel,
    ToolArguments,
    ToolDefinition,
    ToolEffect,
    ToolPolicy,
)


class ExampleArguments(ToolArguments):
    order_id: str = Field(min_length=1)


async def handler(
    arguments: ToolArguments,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    return {"order_id": arguments.model_dump()["order_id"]}


def canonical_target(arguments: ToolArguments, context: ExecutionContext) -> str:
    return f"{context.tenant_id}:order:{arguments.model_dump()['order_id']}"


async def denied_precheck(
    arguments: ToolArguments,
    context: ExecutionContext,
) -> None:
    raise PrecheckDenied("ORDER_NOT_REFUNDABLE")


async def failing_precheck(
    arguments: ToolArguments,
    context: ExecutionContext,
) -> None:
    raise RuntimeError("database password must not leak")


class FixedApprovalVerifier(ApprovalVerifier):
    def __init__(self, approved: bool) -> None:
        self.approved = approved

    async def is_approved(
        self,
        tool: ToolDefinition,
        arguments: ToolArguments,
        context: ExecutionContext,
    ) -> bool:
        return self.approved


class BrokenApprovalVerifier(ApprovalVerifier):
    async def is_approved(
        self,
        tool: ToolDefinition,
        arguments: ToolArguments,
        context: ExecutionContext,
    ) -> bool:
        raise RuntimeError("approval upstream password must not leak")


class PermissionEngineTest(unittest.IsolatedAsyncioTestCase):
    def make_context(
        self,
        *,
        mode: ExecutionMode = ExecutionMode.DEFAULT,
        permissions: frozenset[str] = frozenset({"order:refund"}),
        allow_tools: frozenset[str] = frozenset({"refund_order"}),
        approval_id: str | None = None,
    ) -> ExecutionContext:
        return ExecutionContext(
            trace_id="trace_001",
            user_id="user_001",
            tenant_id="tenant_001",
            permissions=permissions,
            allow_tools=allow_tools,
            mode=mode,
            approval_id=approval_id,
        )

    def make_tool(
        self,
        *,
        effect: ToolEffect = ToolEffect.WRITE,
        requires_confirmation: bool = False,
        requires_approval: bool = False,
        precheck=None,
    ) -> ToolDefinition:
        return ToolDefinition(
            name="refund_order",
            description="退款",
            parameters_model=ExampleArguments,
            tool_policy=ToolPolicy(
                effect=effect,
                risk=RiskLevel.MEDIUM,
                required_permissions=frozenset({"order:refund"}),
                requires_confirmation=requires_confirmation,
                requires_approval=requires_approval,
            ),
            handler=handler,
            precheck=precheck,
            canonical_target=canonical_target,
        )

    async def assert_result(
        self,
        engine: PermissionEngine,
        tool: ToolDefinition,
        context: ExecutionContext,
        decision: PermissionDecision,
        stage: PermissionStage,
        code: str,
    ) -> None:
        result = await engine.decide(tool, ExampleArguments(order_id="ord_001"), context)
        self.assertEqual(result.decision, decision)
        self.assertEqual(result.stage, stage)
        self.assertEqual(result.reason_code, code)

    async def test_plan_rejects_write_before_allow_list_and_rbac(self) -> None:
        await self.assert_result(
            PermissionEngine(),
            self.make_tool(),
            self.make_context(mode=ExecutionMode.PLAN, permissions=frozenset(), allow_tools=frozenset()),
            PermissionDecision.DENY,
            PermissionStage.PLAN_MODE,
            "PLAN_MODE_FORBIDDEN",
        )

    async def test_allow_list_then_rbac_then_precheck(self) -> None:
        engine = PermissionEngine()
        tool = self.make_tool(precheck=denied_precheck)

        await self.assert_result(
            engine,
            tool,
            self.make_context(allow_tools=frozenset()),
            PermissionDecision.DENY,
            PermissionStage.ALLOW_LIST,
            "TOOL_NOT_ALLOWED",
        )
        await self.assert_result(
            engine,
            tool,
            self.make_context(permissions=frozenset()),
            PermissionDecision.DENY,
            PermissionStage.RBAC,
            "PERMISSION_DENIED",
        )
        await self.assert_result(
            engine,
            tool,
            self.make_context(),
            PermissionDecision.DENY,
            PermissionStage.PRECHECK,
            "ORDER_NOT_REFUNDABLE",
        )

    async def test_precheck_unexpected_exception_is_redacted(self) -> None:
        await self.assert_result(
            PermissionEngine(),
            self.make_tool(precheck=failing_precheck),
            self.make_context(),
            PermissionDecision.DENY,
            PermissionStage.PRECHECK,
            "PRECHECK_FAILED",
        )

    async def test_plan_allows_read_after_later_gates_pass(self) -> None:
        await self.assert_result(
            PermissionEngine(),
            self.make_tool(effect=ToolEffect.READ),
            self.make_context(mode=ExecutionMode.PLAN),
            PermissionDecision.ALLOW,
            PermissionStage.FINAL_INTERACTION,
            "ALLOWED",
        )

    async def test_empty_required_permissions_needs_no_capability(self) -> None:
        tool = self.make_tool()
        tool = ToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters_model=tool.parameters_model,
            tool_policy=ToolPolicy(
                effect=ToolEffect.READ,
                risk=RiskLevel.LOW,
                required_permissions=frozenset(),
            ),
            handler=tool.handler,
            precheck=tool.precheck,
            canonical_target=tool.canonical_target,
        )
        await self.assert_result(
            PermissionEngine(),
            tool,
            self.make_context(permissions=frozenset()),
            PermissionDecision.ALLOW,
            PermissionStage.FINAL_INTERACTION,
            "ALLOWED",
        )

    async def test_approval_verifier_failure_fails_closed(self) -> None:
        await self.assert_result(
            PermissionEngine(BrokenApprovalVerifier()),
            self.make_tool(requires_approval=True),
            self.make_context(approval_id="approval_001"),
            PermissionDecision.DENY,
            PermissionStage.REQUIRED_APPROVAL,
            "APPROVAL_INVALID",
        )

    async def test_approval_is_required_and_must_be_verified(self) -> None:
        tool = self.make_tool(requires_approval=True)

        await self.assert_result(
            PermissionEngine(),
            tool,
            self.make_context(),
            PermissionDecision.APPROVAL_REQUIRED,
            PermissionStage.REQUIRED_APPROVAL,
            "APPROVAL_REQUIRED",
        )
        await self.assert_result(
            PermissionEngine(FixedApprovalVerifier(False)),
            tool,
            self.make_context(approval_id="approval_001"),
            PermissionDecision.DENY,
            PermissionStage.REQUIRED_APPROVAL,
            "APPROVAL_INVALID",
        )
        await self.assert_result(
            PermissionEngine(FixedApprovalVerifier(True)),
            tool,
            self.make_context(approval_id="approval_001"),
            PermissionDecision.ALLOW,
            PermissionStage.FINAL_INTERACTION,
            "ALLOWED",
        )

    async def test_confirmation_modes(self) -> None:
        tool = self.make_tool(requires_confirmation=True)
        engine = PermissionEngine()

        await self.assert_result(
            engine,
            tool,
            self.make_context(),
            PermissionDecision.CONFIRM,
            PermissionStage.FINAL_INTERACTION,
            "CONFIRMATION_REQUIRED",
        )
        await self.assert_result(
            engine,
            tool,
            self.make_context(mode=ExecutionMode.BYPASS_PERMISSIONS),
            PermissionDecision.ALLOW,
            PermissionStage.BYPASS_CONFIRMATION,
            "CONFIRMATION_BYPASSED",
        )
        await self.assert_result(
            engine,
            tool,
            self.make_context(mode=ExecutionMode.DONT_ASK),
            PermissionDecision.DENY,
            PermissionStage.FINAL_INTERACTION,
            "CONFIRMATION_UNAVAILABLE",
        )


if __name__ == "__main__":
    unittest.main()
