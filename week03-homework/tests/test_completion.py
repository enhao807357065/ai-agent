"""Final completion evidence and acceptance boundaries."""

from __future__ import annotations

from pathlib import Path

from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def test_final_acceptance_requires_test_evidence_for_testing_fix(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )
    harness.state.add_evidence(harness.run_id, kind="CHANGE", tool_call_id="call_patch", data={"path": "src.py"})

    result = harness.finalize("done")

    assert result["status"] == "failed"
    assert result["stop_reason"] == "FINAL_ACCEPTANCE_FAILED"
    assert "TEST evidence" in result["issues"][0]


def test_code_understanding_cannot_complete_on_final_answer_without_deliverable(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
        prompt="Analyze login code and write artifacts/login-report.md",
    )

    result = harness.finalize("Analysis complete; login is safe.")

    assert result["status"] == "failed"
    assert result["stop_reason"] == "FINAL_ACCEPTANCE_FAILED"
    assert "deliverable" in result["issues"][0].lower()



def test_code_understanding_accepts_deliverable_when_change_evidence_uses_cli_string_output(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
        prompt="Write artifacts/report.md",
    )
    def cli_write(path: str, content: str) -> str:
        harness.sandbox.write_text(path, content)
        return f"wrote {path}"

    harness.register_tool("write_file", {"path": str, "content": str}, cli_write, risk="write")

    harness.gate.invoke(harness.run_id, ToolCall("write-1", "write_file", {"path": "artifacts/report.md", "content": "report"}))

    assert harness.finalize("done")["status"] == "completed"


def test_testing_fix_invalidates_old_passing_test_after_later_source_change(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )
    harness.register_tool("write_file", {"path": str, "content": str}, harness._write_file, risk="write")
    harness.register_tool("run_test", {"argv": list}, lambda argv: {"exit_code": 0}, risk="execute")

    harness.gate.invoke(harness.run_id, ToolCall("change-1", "write_file", {"path": "src.py", "content": "v1\n"}))
    harness.gate.invoke(harness.run_id, ToolCall("test-1", "run_test", {"argv": ["pytest", "-q"]}))
    harness.gate.invoke(harness.run_id, ToolCall("change-2", "write_file", {"path": "src.py", "content": "v2\n"}))

    result = harness.finalize("fixed")

    assert result["status"] == "failed"
    assert result["stop_reason"] == "FINAL_ACCEPTANCE_FAILED"
    assert any("current workspace" in issue for issue in result["issues"])


def test_budget_exhaustion_sets_stop_reason_and_never_claims_success(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
    )
    harness.state.update(harness.run_id, budget={"tool_calls": 0, "max_tool_calls": 0})
    harness.register_tool("read_file", {"path": str}, lambda path: "never", risk="read")

    result = harness.gate.invoke(harness.run_id, ToolCall("over-budget", "read_file", {"path": "src.py"}))

    state = harness.state.get_run(harness.run_id)
    assert result["code"] == "BUDGET_EXHAUSTED"
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "BUDGET_EXHAUSTED"
    assert state["result"] is None
