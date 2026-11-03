"""Sandbox: workspace, command, timeout, environment and output boundaries."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from smolagents.guarded_harness.gate import ToolCall
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy
from smolagents.guarded_harness.sandbox import Sandbox, SandboxViolation


def test_sandbox_blocks_path_command_timeout_and_secret_environment(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sandbox = Sandbox(workspace=workspace, command_allowlist={"python"}, timeout_seconds=0.05, output_limit=20)

    with pytest.raises(SandboxViolation, match="PATH_OUTSIDE_WORKSPACE"):
        sandbox.read_text("../secret.txt")
    with pytest.raises(SandboxViolation, match="COMMAND_NOT_ALLOWED"):
        sandbox.run_command(["sh", "-c", "echo unsafe"])
    with pytest.raises(SandboxViolation, match="COMMAND_TIMED_OUT"):
        sandbox.run_command(["python", "-c", "import time; time.sleep(1)"])

    environment = sandbox.safe_environment({"SECRET_TOKEN": "hidden", "SAFE": "visible"})
    assert environment == {"SAFE": "visible"}



def test_gate_rejects_path_escape_before_write_handler_and_audits(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside_env = tmp_path / ".env"
    outside_env.write_text("unchanged\n")
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.testing_fix(approval_required=False),
    )
    invoked: list[str] = []

    def guarded_write(path: str, content: str) -> None:
        invoked.append(path)
        harness.sandbox.write_text(path, content)

    harness.register_tool("write_file", {"path": str, "content": str}, guarded_write, risk="write")
    result = harness.gate.invoke(
        harness.run_id,
        ToolCall("escape-write", "write_file", {"path": "artifacts/../../.env", "content": "changed\n"}),
    )

    assert result["code"] == "PATH_NOT_ALLOWED"
    assert result["message"] == "SandboxViolation"
    assert invoked == []
    assert outside_env.read_text() == "unchanged\n"
    assert [event["event"] for event in harness.audit.for_tool_call("escape-write")] == ["received", "rejected"]


def test_sandbox_rejects_symlink_escape_and_sensitive_target(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")
    (workspace / "jump").symlink_to(outside, target_is_directory=True)
    (workspace / ".env").write_text("token\n")
    sandbox = Sandbox(workspace=workspace, command_allowlist={"python"})

    with pytest.raises(SandboxViolation, match="PATH_OUTSIDE_WORKSPACE"):
        sandbox.read_text("jump/secret.txt")
    with pytest.raises(SandboxViolation, match="PATH_SENSITIVE_DIRECTORY"):
        sandbox.read_text(".env")


def test_sandbox_lists_and_searches_without_shell(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "login.py").write_text("def verify_password(value: str):\n    return value == 'ok'\n")
    (workspace / ".env").write_text("SECRET=hidden\n")
    sandbox = Sandbox(workspace=workspace, command_allowlist={"pytest"})

    assert sandbox.list_files() == ["src/login.py"]
    assert sandbox.search_text("verify_password") == [
        {"path": "src/login.py", "line": 1, "text": "def verify_password(value: str):"}
    ]



def test_timeout_kills_process_group_child_before_it_can_write(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    marker = workspace / "child-survived.txt"
    sandbox = Sandbox(workspace=workspace, command_allowlist={"python"}, timeout_seconds=0.1)
    # Parent starts a child that would write later, then remains running. Killing
    # only the parent would leave the child to create this marker.
    program = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(0.3); open('child-survived.txt','w').write('alive')\"]); "
        "time.sleep(5)"
    )

    with pytest.raises(SandboxViolation, match="COMMAND_TIMED_OUT"):
        sandbox.run_command(["python", "-c", program])
    time.sleep(0.5)
    assert not marker.exists()


def test_sandbox_runs_command_from_validated_relative_cwd_and_classifies_invalid_runner(tmp_path: Path):
    workspace = tmp_path / "repo"
    project = workspace / "demo-app"
    project.mkdir(parents=True)
    (project / "marker.txt").write_text("ok\n")
    sandbox = Sandbox(workspace=workspace, command_allowlist={"python"})

    result = sandbox.run_command(["python", "-c", "from pathlib import Path; print(Path('marker.txt').read_text().strip())"], cwd="demo-app")
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"

    invalid = sandbox.run_command(["python", "-c", "import sys; sys.stderr.write('No module named test_runner'); sys.exit(1)"], cwd="demo-app")
    assert invalid["failure_kind"] == "TEST_COMMAND_INVALID"
    with pytest.raises(SandboxViolation, match="PATH_OUTSIDE_WORKSPACE"):
        sandbox.run_command(["python", "-c", "print('no')"], cwd="../outside")


def test_sandbox_allows_only_uv_run_allowlisted_command(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sandbox = Sandbox(workspace=workspace, command_allowlist={"pytest"})

    assert sandbox.run_command(["uv", "run", "pytest", "--version"])["exit_code"] == 0
    with pytest.raises(SandboxViolation, match="COMMAND_NOT_ALLOWED"):
        sandbox.run_command(["uv", "run", "sh", "-c", "echo unsafe"])
