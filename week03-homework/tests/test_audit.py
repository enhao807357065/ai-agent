"""Append-only audit reconstruction boundaries."""

from __future__ import annotations

from pathlib import Path

from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def test_audit_reconstructs_decision_chain_by_run_and_tool_call(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(),
    )
    harness.register_tool("read_file", {"path": str}, lambda path: "ok", risk="read")
    harness.gate.invoke(harness.run_id, ToolCall("call_read", "read_file", {"path": "src.py"}))

    chain = harness.audit.for_tool_call("call_read")
    assert [entry["event"] for entry in chain] == ["received", "allowed", "executing", "result"]


def test_rejected_call_audit_has_rule_decision_actor_and_tool_call_id(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(),
    )
    harness.register_tool("read_file", {"path": str}, lambda path: "ok", risk="read")

    harness.gate.invoke(harness.run_id, ToolCall("bad-call", "read_file", {"path": 1}))

    rejected = harness.audit.for_tool_call("bad-call")[-1]
    assert rejected["event"] == "rejected"
    assert rejected["rule"] == "INVALID_ARGUMENT"
    assert rejected["decision"] == "rejected"
    assert rejected["actor"] == "system"
    assert rejected["toolCallId"] == "bad-call"


def test_trace_correlates_gate_sandbox_state_approval_and_result(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )
    harness.register_tool("write_file", {"path": str, "content": str}, lambda path, content: {"path": path}, risk="write")
    harness.gate.invoke(harness.run_id, ToolCall("write-1", "write_file", {"path": "report.md", "content": "ok"}))

    trace = harness.trace()
    assert trace["run_id"] == harness.run_id
    assert trace["state"]["completed_tool_calls"]["write-1"]["ok"] is True
    assert trace["results"]["write-1"]["tool_call_id"] == "write-1"
    assert trace["evidence"][0]["kind"] == "CHANGE"
    assert any(event["event"] == "allowed" for event in trace["gate"])
    assert any(event["event"] == "executing" for event in trace["sandbox"])
    assert trace["approvals"] == []
