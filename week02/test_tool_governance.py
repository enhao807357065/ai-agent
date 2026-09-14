import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "tool_governance_demo",
    str(Path(__file__).with_name("tool_governance_demo.py")),
)
assert spec is not None and spec.loader is not None
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)


class GovernanceRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        demo.ACCOUNTS.clear()
        demo.ACCOUNTS.update({
            ("tenant_a", "ACC-A-123456"): 100_000.0,
            ("tenant_a", "ACC-A-654321"): 5_000.0,
            ("tenant_a", "ACC-A-888888"): 20_000.0,
            ("tenant_b", "ACC-B-111111"): 50_000.0,
        })
        demo.reset_side_effects()
        self.context = demo.base_context(
            permissions=frozenset({"order:read", "refund:create", "shell:run", "transfer:execute"}),
            allowed_tools=frozenset({"get_order", "create_refund", "run_shell", "transfer"}),
        )
        self.runtime, self.approvals, self.audit = demo.build_runtime()

    async def test_transfer_schema_rejects_extra(self) -> None:
        invalid_account = await self.runtime.invoke(
            demo.ToolCall("schema-1", "transfer", {
                "from_account": "INVALID", "to_account": "ACC-A-654321", "amount": 100,
            }),
            self.context,
        )
        extra_field = await self.runtime.invoke(
            demo.ToolCall("schema-2", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100,
                "approved": True,
            }),
            self.context,
        )
        self.assertEqual(invalid_account.code, "INVALID_ARGUMENT")
        self.assertEqual(extra_field.code, "INVALID_ARGUMENT")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_precheck_insufficient(self) -> None:
        result = await self.runtime.invoke(
            demo.ToolCall("insufficient", "transfer", {
                "from_account": "ACC-A-654321", "to_account": "ACC-A-123456", "amount": 6000,
            }),
            self.context,
        )
        self.assertEqual(result.code, "INSUFFICIENT_BALANCE")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_precheck_exceed_limit(self) -> None:
        result = await self.runtime.invoke(
            demo.ToolCall("limit", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 60000,
            }),
            self.context,
        )
        self.assertEqual(result.code, "EXCEED_LIMIT")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_approval_binding(self) -> None:
        approved = demo.TransferArgs(
            from_account="ACC-A-123456", to_account="ACC-A-654321", amount=100,
        )
        self.approvals.approve("approval-100", self.context, "transfer", approved)
        result = await self.runtime.invoke(
            demo.ToolCall("changed-amount", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 200,
            }),
            demo.replace(self.context, approval_id="approval-100"),
        )
        self.assertEqual(result.action, demo.DecisionAction.CONFIRM)
        self.assertEqual(result.code, "APPROVAL_REQUIRED")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_timeout_no_retry(self) -> None:
        args = demo.TransferArgs(
            from_account="ACC-A-123456", to_account="ACC-A-654321", amount=45000,
        )
        self.approvals.approve("approval-45k", self.context, "transfer", args)
        result = await self.runtime.invoke(
            demo.ToolCall("timeout-45k", "transfer", args.model_dump()),
            demo.replace(self.context, approval_id="approval-45k"),
        )
        self.assertEqual(result.code, "TIMEOUT_UNKNOWN")
        self.assertLessEqual(demo.SIDE_EFFECTS["transfer_executions"], 1)

    async def test_audit_and_confirm_are_observable(self) -> None:
        pending = await self.runtime.invoke(
            demo.ToolCall("confirm-1", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100,
            }),
            self.context,
        )
        self.assertEqual(pending.action, demo.DecisionAction.CONFIRM)
        self.assertEqual(pending.code, "APPROVAL_REQUIRED")

        approved = demo.TransferArgs(
            from_account="ACC-A-123456", to_account="ACC-A-654321", amount=100,
        )
        self.approvals.approve("approval-audit", self.context, "transfer", approved)
        completed = await self.runtime.invoke(
            demo.ToolCall("audit-1", "transfer", approved.model_dump()),
            demo.replace(self.context, approval_id="approval-audit"),
        )
        self.assertEqual(completed.code, "OK")
        self.assertEqual(completed.content["from"], "ACC-A-****3456")
        self.assertEqual(completed.content["to"], "ACC-A-****4321")
        print("audit:", self.audit.records)
        print("confirmation:", pending.action, pending.code)
        print("redacted result:", completed.content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
