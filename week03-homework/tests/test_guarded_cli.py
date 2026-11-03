"""Formal ``smolagent guarded`` command tests without a real model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smolagents.guarded_harness.cli import _repository_tools, main
from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def create_waiting_run(tmp_path: Path) -> tuple[GuardedCodingHarness, str]:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "guarded.sqlite",
        task=TaskPolicy.testing_fix(approval_required=True),
    )
    approval = {"approval_id": "approval-1", "tool_call_id": "call-write", "tool": "write_file", "arguments": {"path": "x.py", "content": "x = 1\n"}, "risk": "write", "status": "pending"}
    harness.state.add_approval(harness.run_id, approval)
    harness.checkpoint.save(harness.run_id, reason="waiting_approval")
    return harness, str(harness.database)




def test_create_makes_missing_workspace_before_opening_database(tmp_path: Path):
    workspace = tmp_path / "new-repo"
    database = workspace / ".guarded-harness.sqlite"

    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=database,
        task=TaskPolicy.code_understanding(),
    )

    assert workspace.is_dir()
    assert database.is_file()
    assert harness.workspace == workspace.resolve()


def test_understand_cli_exposes_readonly_discovery_tools_only(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "guarded.sqlite",
        task=TaskPolicy.code_understanding(),
    )

    tools = _repository_tools(harness)

    assert [tool.name for tool in tools] == ["list_files", "search_text", "read_file", "write_file"]


def test_understand_cli_no_approval_uses_non_approving_policy(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    captured = {}

    def fake_model(args):
        class UnusedModel:
            pass
        return UnusedModel()

    def fake_agent(*, model, tools, **kwargs):
        captured["approval_required"] = harness.policy.approval_required
        return type("Agent", (), {"run": lambda self, prompt: "done"})()

    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "guarded.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
    )
    monkeypatch.setattr("smolagents.guarded_harness.cli._model", fake_model)
    monkeypatch.setattr("smolagents.guarded_harness.cli.GuardedCodingHarness.create", lambda **kwargs: harness)
    monkeypatch.setattr(harness, "build_agent", fake_agent)

    assert main(["understand", "--workspace", str(workspace), "--no-approval", "analyze"]) == 0
    assert captured["approval_required"] is False




def test_fix_cli_injects_explicit_tool_call_protocol(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    captured = {}
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "guarded.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )

    monkeypatch.setattr("smolagents.guarded_harness.cli._model", lambda args: object())
    monkeypatch.setattr("smolagents.guarded_harness.cli.GuardedCodingHarness.create", lambda **kwargs: harness)

    def fake_agent(*, model, tools, **kwargs):
        captured["instructions"] = kwargs["instructions"]
        return type("Agent", (), {"run": lambda self, prompt: "done"})()

    monkeypatch.setattr(harness, "build_agent", fake_agent)

    assert main(["fix", "--workspace", str(workspace), "--no-approval", "repair"]) == 0
    assert "native function call" in captured["instructions"]
    assert "Never emit prose" in captured["instructions"]
    assert "run_test" in captured["instructions"]
    assert "package.json" in captured["instructions"]
    assert "run_test.cwd" in captured["instructions"]
    assert "TEST_COMMAND_INVALID" in captured["instructions"]
    assert "TEST_FAILED" in captured["instructions"]
    assert "final_answer" in captured["instructions"]


def test_testing_fix_policy_allows_node_project_test_runners():
    policy = TaskPolicy.testing_fix(approval_required=False)

    assert {"node", "npm", "npx"}.issubset(policy.command_allowlist)


def test_deepseek_model_forces_tool_choice_after_disabling_thinking(monkeypatch):
    captured = {}

    class FakeOpenAIModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("smolagents.guarded_harness.cli.OpenAIModel", FakeOpenAIModel)
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    args = type(
        "Args",
        (),
        {
            "model_id": "deepseek-v4-flash",
            "api_base": "https://api.deepseek.com/v1",
            "deepseek_thinking": "disabled",
        },
    )()

    from smolagents.guarded_harness.cli import _model

    _model(args)

    assert captured["tool_choice"] == "required"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_cli_run_test_accepts_workspace_relative_cwd(tmp_path: Path):
    workspace = tmp_path / "repo"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "marker.txt").write_text("ok\n")
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "guarded.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )
    _repository_tools(harness)

    result = harness.gate.invoke(
        harness.run_id,
        ToolCall(
            "run-in-project",
            "run_test",
            {"argv": ["python", "-c", "from pathlib import Path; print(Path('marker.txt').read_text().strip())"], "cwd": "project"},
        ),
    )

    assert result["ok"] is True
    assert result["content"]["stdout"].strip() == "ok"
    assert result["content"]["failure_kind"] is None


def test_guarded_cli_show_and_audit_print_durable_run(tmp_path: Path, capsys):
    harness, database = create_waiting_run(tmp_path)

    assert main(["show", "--run-id", harness.run_id, "--workspace", str(harness.workspace), "--database", database]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["run_id"] == harness.run_id
    assert state["pending_approval"] == "approval-1"

    assert main(["audit", "--run-id", harness.run_id, "--workspace", str(harness.workspace), "--database", database]) == 0
    events = json.loads(capsys.readouterr().out)
    assert events[0]["event"] == "run_created"


def test_guarded_cli_approve_and_resume_consumes_frozen_action_once(tmp_path: Path, capsys):
    harness, database = create_waiting_run(tmp_path)

    assert main([
        "approve", "--run-id", harness.run_id, "--approval-id", "approval-1", "--actor", "reviewer",
        "--workspace", str(harness.workspace), "--database", database,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["approved"] is True

    # The formal command deliberately resumes only one frozen action. Because this
    # fixture has no registered write handler after process recreation, Gate emits
    # a structured tool result rather than replaying an uncontrolled write.
    assert main([
        "resume", "--run-id", harness.run_id, "--approval-id", "approval-1",
        "--workspace", str(harness.workspace), "--database", database,
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["executed"]["tool_call_id"] == "call-write"
