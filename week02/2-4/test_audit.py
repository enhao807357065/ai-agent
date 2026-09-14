import unittest

from tool_models import (
    AuditPhase,
    AuditRecord,
    AuditSink,
    PermissionDecision,
)


class AuditSinkTest(unittest.TestCase):
    def test_denied_call_has_only_decision_record(self) -> None:
        sink = AuditSink()
        decision_record = AuditRecord(
            trace_id="trace_denied",
            tool_call_id="call_001",
            tool_name="refund_order",
            user_id="user_001",
            tenant_id="tenant_001",
            phase=AuditPhase.DECISION,
            decision=PermissionDecision.DENY,
            code="PERMISSION_DENIED",
            argument_keys=frozenset({"order_id", "amount"}),
        )

        sink.append(decision_record)

        self.assertEqual(sink.by_trace("trace_denied"), (decision_record,))
        self.assertEqual(
            sink.by_trace("trace_denied")[0].phase,
            AuditPhase.DECISION,
        )

    def test_successful_call_has_decision_and_execution_records(self) -> None:
        sink = AuditSink()
        decision_record = AuditRecord(
            trace_id="trace_success",
            tool_call_id="call_002",
            tool_name="get_order",
            user_id="user_001",
            tenant_id="tenant_001",
            phase=AuditPhase.DECISION,
            decision=PermissionDecision.ALLOW,
            code="ALLOWED",
            argument_keys=frozenset({"order_id"}),
        )
        execution_record = AuditRecord(
            trace_id="trace_success",
            tool_call_id="call_002",
            tool_name="get_order",
            user_id="user_001",
            tenant_id="tenant_001",
            phase=AuditPhase.EXECUTION,
            decision=PermissionDecision.ALLOW,
            code="EXECUTED",
            argument_keys=frozenset({"order_id"}),
            latency_ms=12,
        )

        sink.append(decision_record)
        sink.append(execution_record)

        self.assertEqual(
            sink.by_trace("trace_success"),
            (decision_record, execution_record),
        )


if __name__ == "__main__":
    unittest.main()
