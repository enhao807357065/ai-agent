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
                "from_account": "INVALID", "to_account": "ACC-A-654321", "amount": 100.0,
            }),
            self.context,
        )
        extra_field = await self.runtime.invoke(
            demo.ToolCall("schema-2", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0,
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
                "from_account": "ACC-A-654321", "to_account": "ACC-A-123456", "amount": 6000.0,
            }),
            self.context,
        )
        self.assertEqual(result.code, "INSUFFICIENT_BALANCE")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_precheck_exceed_limit(self) -> None:
        result = await self.runtime.invoke(
            demo.ToolCall("limit", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 60000.0,
            }),
            self.context,
        )
        self.assertEqual(result.code, "EXCEED_LIMIT")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_approval_binding_rejects_different_call_or_arguments(self) -> None:
        original = demo.ToolCall("approved-call", "transfer", {
            "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0,
        })
        pending = await self.runtime.invoke(original, self.context)
        self.assertEqual(pending.action, demo.DecisionAction.CONFIRM)
        self.approvals.approve("approval-100", self.runtime._pending[original.tool_call_id])
        result = await self.runtime.invoke(
            demo.ToolCall("changed-amount", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 200.0,
            }),
            demo.replace(self.context, approval_id="approval-100"),
        )
        self.assertEqual(result.action, demo.DecisionAction.CONFIRM)
        self.assertEqual(result.code, "APPROVAL_REQUIRED")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)

    async def test_transfer_timeout_no_retry(self) -> None:
        call = demo.ToolCall("timeout-45k", "transfer", {
            "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 45000.0,
        })
        pending = await self.runtime.invoke(call, self.context)
        self.assertEqual(pending.action, demo.DecisionAction.CONFIRM)
        result = await self.runtime.confirm("approval-45k", call.tool_call_id)
        self.assertEqual(result.code, "TIMEOUT_UNKNOWN")
        self.assertLessEqual(demo.SIDE_EFFECTS["transfer_executions"], 1)

    async def test_audit_and_confirm_are_observable(self) -> None:
        pending = await self.runtime.invoke(
            demo.ToolCall("confirm-1", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0,
            }),
            self.context,
        )
        self.assertEqual(pending.action, demo.DecisionAction.CONFIRM)
        self.assertEqual(pending.code, "APPROVAL_REQUIRED")

        completed = await self.runtime.confirm("approval-audit", "confirm-1")
        self.assertEqual(completed.code, "OK")
        self.assertEqual(completed.content["from"], "ACC-A-****3456")
        self.assertEqual(completed.content["to"], "ACC-A-****4321")

    async def test_confirm_resumes_exact_original_call_and_normalizes_arguments(self) -> None:
        call = demo.ToolCall("original-100", "transfer", {
            "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0,
        })
        pending = await self.runtime.invoke(call, self.context)
        self.assertEqual(pending.action, demo.DecisionAction.CONFIRM)
        completed = await self.runtime.confirm("approval-original", call.tool_call_id)
        self.assertEqual(completed.code, "OK")
        self.assertEqual(completed.tool_call_id, "original-100")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 1)

    async def test_precheck_unknown_error_fails_closed(self) -> None:
        async def broken_precheck(_args, _context):
            raise RuntimeError("database password=do-not-leak")

        registry = self._registry_with_override("transfer", precheck=broken_precheck)
        runtime, _, _ = demo.build_runtime(registry=registry, audit=self.audit)
        result = await runtime.invoke(
            demo.ToolCall("broken-precheck", "transfer", {
                "from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0,
            }),
            self.context,
        )
        self.assertEqual(result.code, "PRECHECK_FAILED")
        self.assertEqual(demo.SIDE_EFFECTS["transfer_executions"], 0)
        self.assertEqual(self.audit.records[-1].phase, "decision")

    async def test_output_validation_rejects_undeclared_sensitive_field(self) -> None:
        async def leaking_handler(_call_id, _args, _context):
            return {
                "status": "paid",
                "refundable": 399.0,
                "customer_email": "alice@example.com",
                "access_token": "tok_demo_should_not_leak",
            }

        runtime, _, _ = demo.build_runtime(
            registry=self._registry_with_override("get_order", handler=leaking_handler)
        )
        result = await runtime.invoke(
            demo.ToolCall("order-output", "get_order", {"order_id": "ord_1001"}), self.context
        )
        self.assertEqual(result.code, "INVALID_TOOL_OUTPUT")
        self.assertIsInstance(result.content, demo.ToolError)

    async def test_unexpected_handler_error_uses_safe_tool_error(self) -> None:
        async def broken_handler(_call_id, _args, _context):
            raise RuntimeError("postgres://admin:secret@internal.example/db")

        runtime, _, _ = demo.build_runtime(
            registry=self._registry_with_override("get_order", handler=broken_handler)
        )
        result = await runtime.invoke(
            demo.ToolCall("broken-handler", "get_order", {"order_id": "ord_1001"}), self.context
        )
        self.assertEqual(result.code, "TOOL_ERROR")
        self.assertEqual(result.content.message, "工具执行失败")
        self.assertNotIn("secret", result.content.message)

    async def test_new_snapshot_uses_new_route_but_old_snapshot_is_immutable(self) -> None:
        registry = demo.build_registry(versions=("v1", "v2"))
        old_runtime, _, _ = demo.build_runtime(registry=registry, routes={"get_order": "v1"})
        new_runtime, _, _ = demo.build_runtime(registry=registry, routes={"get_order": "v2"})
        self.assertEqual(old_runtime._snapshot.routes["get_order"].version, "v1")
        self.assertEqual(new_runtime._snapshot.routes["get_order"].version, "v2")
        self.assertNotIn("v1", old_runtime.model_tools(self.context)[0]["function"]["name"])
        with self.assertRaises(TypeError):
            old_runtime._snapshot.routes["get_order"] = new_runtime._snapshot.routes["get_order"]

    async def test_version_switch_blocks_stale_snapshot_but_not_new_version(self) -> None:
        registry = demo.build_registry(versions=("v1", "v2"))
        old_runtime, _, _ = demo.build_runtime(registry=registry, routes={"get_order": "v1"})
        new_runtime, _, _ = demo.build_runtime(registry=registry, routes={"get_order": "v2"})
        registry.set_version_enabled("get_order", "v1", False)

        stale = await old_runtime.invoke(
            demo.ToolCall("stale-v1", "get_order", {"order_id": "ord_1001"}), self.context
        )
        current = await new_runtime.invoke(
            demo.ToolCall("live-v2", "get_order", {"order_id": "ord_1001"}), self.context
        )
        self.assertEqual(stale.code, "TOOL_DISABLED")
        self.assertEqual(current.code, "OK")

    async def test_family_switch_blocks_all_versions_and_new_snapshot_rejects_disabled_route(self) -> None:
        registry = demo.build_registry(versions=("v1", "v2"))
        runtime, _, _ = demo.build_runtime(registry=registry, routes={"get_order": "v2"})
        registry.set_family_enabled("get_order", False)

        result = await runtime.invoke(
            demo.ToolCall("family-off", "get_order", {"order_id": "ord_1001"}), self.context
        )
        self.assertEqual(result.code, "TOOL_DISABLED")
        with self.assertRaisesRegex(ValueError, "不可路由"):
            demo.build_runtime(registry=registry, routes={"get_order": "v1"})

    def _registry_with_override(self, name: str, **changes):
        registry = demo.ToolRegistry()
        for tool in demo.build_tools():
            registry.register(demo.replace(tool, **changes) if tool.name == name else tool)
        return registry


if __name__ == "__main__":
    unittest.main(verbosity=2)
