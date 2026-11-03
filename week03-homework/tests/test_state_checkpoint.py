"""Durable StateStore and CheckpointStore recovery boundaries."""

from __future__ import annotations

from pathlib import Path

from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def test_checkpoint_resume_does_not_replay_completed_write_and_restores_business_state(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    database = tmp_path / "harness.sqlite"
    harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=TaskPolicy.testing_fix(approval_required=False))
    calls = []
    harness.register_tool("write_marker", {"value": str}, lambda value: calls.append(value) or {"written": value}, risk="write")

    result = harness.gate.invoke(harness.run_id, ToolCall("call_once", "write_marker", {"value": "v1"}))
    assert result["ok"] is True
    harness.state.add_step(harness.run_id, "fix", "completed")
    harness._checkpoint("safe_tool_boundary")

    resumed = GuardedCodingHarness.from_checkpoint(database, harness.run_id, workspace=workspace)

    restored = resumed.state.get_run(harness.run_id)
    assert restored["steps"] == [{"name": "fix", "status": "completed"}]
    assert restored["budget"]["tool_calls"] == 1
    assert restored["completed_tool_calls"]["call_once"]["ok"] is True
    assert resumed.resume() is None
    assert calls == ["v1"]
    assert resumed.audit.for_tool_call("call_once")[-1]["event"] == "result"


def test_gate_saves_checkpoint_after_result_and_pending_approval(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    database = tmp_path / "harness.sqlite"
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=database,
        task=TaskPolicy.testing_fix(approval_required=True),
        prompt="fix the test",
    )
    harness.state.add_step(harness.run_id, "inspect", "completed")
    harness.state.update(harness.run_id, plan=[{"id": "step-1"}])
    harness.register_tool("read", {"path": str}, lambda path: "source", risk="read")
    harness.register_tool("write", {"path": str, "content": str}, lambda path, content: "written", risk="write")

    harness.gate.invoke(harness.run_id, ToolCall("read-1", "read", {"path": "src.py"}))
    after_result = harness.checkpoint.load(harness.run_id)
    assert after_result["run_id"] == harness.run_id
    assert after_result["steps"] == [{"name": "inspect", "status": "completed"}]
    assert after_result["plan"] == [{"id": "step-1"}]
    assert after_result["budget"]["tool_calls"] == 1
    assert after_result["evidence"][0]["tool_call_id"] == "read-1"

    pending = harness.gate.invoke(harness.run_id, ToolCall("write-1", "write", {"path": "out.md", "content": "report"}))
    after_approval = harness.checkpoint.load(harness.run_id)
    assert pending["code"] == "APPROVAL_REQUIRED"
    assert after_approval["pending_approval"] == pending["approval_id"]
    assert after_approval["approvals"][0]["tool_call_id"] == "write-1"




def test_recovery_marks_started_write_without_result_unknown_and_never_replays(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    database = tmp_path / "harness.sqlite"
    harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=TaskPolicy.testing_fix(approval_required=False))
    harness.state.update(
        harness.run_id,
        in_flight_tool_call={
            "tool_call_id": "write-crashed",
            "tool": "write_file",
            "arguments": {"path": "src.py", "content": "new"},
            "risk": "write",
        },
    )
    harness._checkpoint("tool_execution_started")
    # Simulate the crash window: the file could have changed, but no result was
    # persisted. Recovery must not call a write handler to find out.
    (workspace / "src.py").write_text("maybe written\n")

    recovered = GuardedCodingHarness.from_checkpoint(database, harness.run_id, workspace=workspace)

    state = recovered.state.get_run(harness.run_id)
    assert state["status"] == "waiting_verification"
    assert state["stop_reason"] == "UNKNOWN_RESULT"
    assert state["in_flight_tool_call"]["status"] == "UNKNOWN_RESULT"
    assert state["completed_tool_calls"] == {}
    assert recovered.resume() is None
    assert any(event["event"] == "unknown_result" for event in recovered.audit.for_run(harness.run_id))


def test_recovery_stops_when_workspace_changed_after_checkpoint(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    database = tmp_path / "harness.sqlite"
    harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=TaskPolicy.code_understanding())
    harness.register_repository_tools()
    pending = harness.gate.invoke(
        harness.run_id,
        ToolCall("write-1", "write_file", {"path": "artifacts/report.md", "content": "report"}),
    )
    (workspace / "external-change.txt").write_text("changed outside harness\n")

    recovered = GuardedCodingHarness.from_checkpoint(database, harness.run_id, workspace=workspace)

    state = recovered.state.get_run(harness.run_id)
    assert pending["code"] == "APPROVAL_REQUIRED"
    assert state["status"] == "waiting_verification"
    assert state["stop_reason"] == "WORKSPACE_CHANGED"
    assert recovered.approve(pending["approval_id"], actor="reviewer") is False
    assert recovered.resume_approval(pending["approval_id"]) is None
    assert any(event["event"] == "workspace_changed" for event in recovered.audit.for_run(harness.run_id))


def test_recovery_uses_live_state_when_no_checkpoint_has_been_written(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    database = tmp_path / "harness.sqlite"
    harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=TaskPolicy.code_understanding())
    harness.state.add_approval(
        harness.run_id,
        {
            "approval_id": "approval-live",
            "tool_call_id": "call-live",
            "tool": "write_file",
            "arguments": {"path": "artifacts/report.md", "content": "report"},
            "risk": "write",
            "status": "pending",
        },
    )

    recovered = GuardedCodingHarness.from_checkpoint(database, harness.run_id, workspace=workspace)

    assert recovered.state.get_run(harness.run_id)["pending_approval"] == "approval-live"
    assert recovered.checkpoint.load(harness.run_id)["run_id"] == harness.run_id
    assert any(event["event"] == "checkpoint_created_from_live_state" for event in recovered.audit.for_run(harness.run_id))
