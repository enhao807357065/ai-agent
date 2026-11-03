"""Gate: parameter, task-risk, approval and duplicate-call boundaries."""

from __future__ import annotations

from pathlib import Path

from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def make_harness(tmp_path: Path, *, approval_required: bool = False, task: TaskPolicy | None = None) -> GuardedCodingHarness:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src.py").write_text("def add(a, b):\n    return a - b\n")
    return GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=task or TaskPolicy.testing_fix(approval_required=approval_required),
    )


def test_gate_rejects_invalid_parameters_before_handler_and_keeps_tool_call_id(tmp_path: Path):
    harness = make_harness(tmp_path)
    invoked = []
    harness.register_tool("read_file", {"path": str}, lambda path: invoked.append(path) or "data", risk="read")

    missing = harness.gate.invoke(harness.run_id, ToolCall("call_missing", "read_file", {}))
    wrong_type = harness.gate.invoke(harness.run_id, ToolCall("call_bad", "read_file", {"path": 42}))

    assert missing == {"ok": False, "tool_call_id": "call_missing", "code": "INVALID_ARGUMENT", "message": "path must be str"}
    assert wrong_type == {"ok": False, "tool_call_id": "call_bad", "code": "INVALID_ARGUMENT", "message": "path must be str"}
    assert invoked == []
    assert [entry["event"] for entry in harness.audit.for_tool_call("call_bad")] == ["received", "rejected"]


def test_gate_blocks_third_identical_tool_and_arguments_before_handler(tmp_path: Path):
    harness = make_harness(tmp_path)
    invocations: list[str] = []
    harness.register_tool("read_file", {"path": str}, lambda path: invocations.append(path) or "content", risk="read")

    first = harness.gate.invoke(harness.run_id, ToolCall("call-1", "read_file", {"path": "src.py"}))
    second = harness.gate.invoke(harness.run_id, ToolCall("call-2", "read_file", {"path": "src.py"}))
    third = harness.gate.invoke(harness.run_id, ToolCall("call-3", "read_file", {"path": "src.py"}))

    fourth = harness.gate.invoke(harness.run_id, ToolCall("call-4", "read_file", {"path": "other.py"}))

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["code"] == "REPEATED_TOOL_ACTION"
    assert "twice consecutively" in third["message"]
    assert fourth["code"] == "REPEATED_TOOL_ACTION"
    stopped = harness.state.get_run(harness.run_id)
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "REPEATED_TOOL_ACTION"
    assert invocations == ["src.py", "src.py"]



def test_gate_allows_repeated_test_after_nonconsecutive_actions(tmp_path: Path):
    harness = make_harness(tmp_path)
    executions: list[str] = []
    harness.register_tool("run_test", {"argv": list}, lambda argv: executions.append("test") or {"exit_code": 0}, risk="execute")
    harness.register_tool("read_file", {"path": str}, lambda path: executions.append("read") or "content", risk="read")
    test_call = {"argv": ["pytest", "-q"]}

    assert harness.gate.invoke(harness.run_id, ToolCall("test-first", "run_test", test_call))["ok"] is True
    assert harness.gate.invoke(harness.run_id, ToolCall("read-between", "read_file", {"path": "src.py"}))["ok"] is True
    assert harness.gate.invoke(harness.run_id, ToolCall("test-second", "run_test", test_call))["ok"] is True
    assert executions == ["test", "read", "test"]
    assert harness.state.get_run(harness.run_id)["status"] == "running"


def test_gate_stops_third_consecutive_test_call(tmp_path: Path):
    harness = make_harness(tmp_path)
    executions: list[str] = []
    harness.register_tool("run_test", {"argv": list}, lambda argv: executions.append("test") or {"exit_code": 0}, risk="execute")
    test_call = {"argv": ["pytest", "-q"]}

    assert harness.gate.invoke(harness.run_id, ToolCall("test-1", "run_test", test_call))["ok"] is True
    assert harness.gate.invoke(harness.run_id, ToolCall("test-2", "run_test", test_call))["ok"] is True
    rejected = harness.gate.invoke(harness.run_id, ToolCall("test-3", "run_test", test_call))

    assert rejected["code"] == "REPEATED_TOOL_ACTION"
    assert executions == ["test", "test"]



def test_gate_stops_after_two_consecutive_invalid_test_commands(tmp_path: Path):
    harness = make_harness(tmp_path)
    invocations: list[list[str]] = []
    harness.register_tool(
        "run_test",
        {"argv": list},
        lambda argv: invocations.append(argv) or {"exit_code": 1, "failure_kind": "TEST_COMMAND_INVALID"},
        risk="execute",
    )

    assert harness.gate.invoke(harness.run_id, ToolCall("bad-1", "run_test", {"argv": ["pytest", "missing-1"]}))["ok"] is True
    assert harness.gate.invoke(harness.run_id, ToolCall("bad-2", "run_test", {"argv": ["pytest", "missing-2"]}))["ok"] is True
    rejected = harness.gate.invoke(harness.run_id, ToolCall("bad-3", "run_test", {"argv": ["pytest", "missing-3"]}))

    assert rejected["code"] == "TEST_COMMAND_CONFIGURATION_UNRESOLVED"
    assert invocations == [["pytest", "missing-1"], ["pytest", "missing-2"]]
    state = harness.state.get_run(harness.run_id)
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "TEST_COMMAND_CONFIGURATION_UNRESOLVED"


def test_code_understanding_defers_write_until_approval(tmp_path: Path):
    harness = make_harness(tmp_path, task=TaskPolicy.code_understanding())
    invoked = []
    harness.register_tool("write_file", {"path": str, "content": str}, lambda **kwargs: invoked.append(kwargs), risk="write")

    result = harness.gate.invoke(harness.run_id, ToolCall("call_write", "write_file", {"path": "src.py", "content": "report"}))

    assert result["code"] == "APPROVAL_REQUIRED"
    assert invoked == []
    assert harness.approve(result["approval_id"], actor="reviewer") is True
    resumed = harness.resume()
    assert resumed is not None and resumed["ok"] is True
    assert invoked == [{"path": "src.py", "content": "report"}]


def test_handler_failure_returns_structured_error_and_keeps_tool_call_id(tmp_path: Path):
    harness = make_harness(tmp_path)

    def fail(argv: list[str]) -> None:
        raise RuntimeError("COMMAND_NOT_ALLOWED")

    harness.register_tool("run_test", {"argv": list}, fail, risk="execute")
    result = harness.gate.invoke(harness.run_id, ToolCall("call_command", "run_test", {"argv": ["uv", "run", "pytest"]}))

    assert result == {
        "ok": False,
        "tool_call_id": "call_command",
        "code": "TOOL_EXECUTION_FAILED",
        "message": "RuntimeError",
    }
    assert [event["event"] for event in harness.audit.for_tool_call("call_command")] == [
        "received", "allowed", "executing", "result"
    ]


def test_write_requires_approval_then_executes_once(tmp_path: Path):
    harness = make_harness(tmp_path, approval_required=True)
    harness.register_repository_tools()
    call = ToolCall("call_write", "write_file", {"path": "src.py", "content": "fixed\n"})

    pending = harness.gate.invoke(harness.run_id, call)
    assert pending["code"] == "APPROVAL_REQUIRED"
    assert (harness.workspace / "src.py").read_text().startswith("def add")
    assert harness.approve(pending["approval_id"], actor="reviewer") is True

    executed = harness.resume()
    assert executed is not None and executed["tool_call_id"] == "call_write"
    assert executed["ok"] is True
    assert (harness.workspace / "src.py").read_text() == "fixed\n"
    assert harness.gate.invoke(harness.run_id, call)["code"] == "REPEATED_TOOL_CALL"
