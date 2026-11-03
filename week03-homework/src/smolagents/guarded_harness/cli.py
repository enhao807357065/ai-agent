"""Formal ``smolagent guarded`` command for guarded repository tasks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from smolagents import OpenAIModel, tool

from .harness import GuardedCodingHarness, TaskPolicy


def _database(workspace: Path, value: str | None) -> Path:
    return Path(value).resolve() if value else (workspace / ".guarded-harness.sqlite").resolve()


def _model(args: argparse.Namespace) -> OpenAIModel:
    api_key = os.getenv("GATEWAY_API_KEY")
    if not api_key:
        raise RuntimeError("GATEWAY_API_KEY must be set in the environment.")
    model_kwargs: dict[str, Any] = {"temperature": 0}
    # smolagents defaults to tool_choice="required". DeepSeek thinking mode
    # rejects required/named tool selection, so turn thinking off for governed
    # multi-turn runs. Keep required afterward: the ToolCallingAgent protocol
    # needs an actual function call each turn, not prose that JSON parsing must
    # recover from.
    if "deepseek" in args.model_id.lower():
        model_kwargs["tool_choice"] = "required"
        if args.deepseek_thinking == "disabled":
            model_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return OpenAIModel(
        model_id=args.model_id,
        api_base=args.api_base,
        api_key=api_key,
        **model_kwargs,
    )


def _repository_tools(harness: GuardedCodingHarness) -> list[Any]:
    """Expose the smallest tool surface allowed by the selected task policy."""
    @tool
    def list_files(path: str = ".") -> list[str]:
        """List workspace-relative files without invoking a shell.

        Args:
            path: Directory path relative to the repository workspace.
        """
        return harness.sandbox.list_files(path)

    @tool
    def search_text(query: str, path: str = ".") -> list[dict]:
        """Search plain text files without invoking a shell.

        Args:
            query: Literal text to search for.
            path: Directory path relative to the repository workspace.
        """
        return harness.sandbox.search_text(query, path)

    @tool
    def read_file(path: str) -> str:
        """Read one workspace-relative text file.

        Args:
            path: Path relative to the repository workspace.
        """
        return harness.sandbox.read_text(path)

    readonly_tools = [list_files, search_text, read_file]
    harness.register_tool("list_files", {"path": str}, list_files, risk="read")
    harness.register_tool("search_text", {"query": str, "path": str}, search_text, risk="read")
    harness.register_tool("read_file", {"path": str}, read_file, risk="read")

    @tool
    def write_file(path: str, content: str) -> str:
        """Write one workspace-relative text file.

        Args:
            path: Path relative to the repository workspace.
            content: Complete replacement content for the file.
        """
        harness.sandbox.write_text(path, content)
        return f"wrote {path}"

    harness.register_tool("write_file", {"path": str, "content": str}, write_file, risk="write")
    if harness.policy.kind == "code_understanding":
        return [*readonly_tools, write_file]

    @tool
    def run_test(argv: list[str], cwd: str = ".") -> dict:
        """Run one allowlisted repository test or check command.

        Args:
            argv: Command argv. First item must be an approved repository runner
                such as python, pytest, node, npm, npx, or git.
            cwd: Workspace-relative project directory for command execution.
        """
        return harness.sandbox.run_command(argv, cwd=cwd)

    harness.register_tool("run_test", {"argv": list, "cwd": str}, run_test, risk="execute")
    return [*readonly_tools, write_file, run_test]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smolagent guarded", description="Run guarded repository tasks")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_run(kind: str) -> None:
        command = commands.add_parser(kind, help=f"start a guarded {kind} run")
        command.add_argument("prompt")
        command.add_argument("--workspace", required=True, type=Path)
        command.add_argument("--database")
        command.add_argument("--model-id", default=os.getenv("GATEWAY_MODEL_ID", "chat-default"))
        command.add_argument("--api-base", default=os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8000/v1"))
        command.add_argument("--max-steps", type=int, default=20)
        command.add_argument(
            "--deepseek-thinking",
            choices=("disabled", "enabled"),
            default="disabled",
            help="DeepSeek only: disable thinking by default for multi-turn tool calling",
        )
        if kind == "understand":
            command.add_argument("--no-approval", action="store_true", help="local learning only: do not pause report writes")
        elif kind == "fix":
            command.add_argument("--no-approval", action="store_true", help="local learning only: do not pause write/execute calls")

    add_run("understand")
    add_run("fix")

    approve = commands.add_parser("approve", help="approve exactly one frozen action by approval ID")
    approve.add_argument("--approval-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--database", required=True)
    # Compatibility-only selectors. Approval ID remains the authority; supplied
    # values are checked against server-owned state and never used to select it.
    approve.add_argument("--run-id", help=argparse.SUPPRESS)
    approve.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)

    resume = commands.add_parser("resume", help="execute exactly one approved action by approval ID")
    resume.add_argument("--approval-id", required=True)
    resume.add_argument("--database", required=True)
    resume.add_argument("--run-id", help=argparse.SUPPRESS)
    resume.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)

    for name, help_text in (("show", "show durable business state"), ("audit", "show append-only audit chain"), ("trace", "show complete Gate/Sandbox/State/Approval evidence")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-id", required=True)
        command.add_argument("--workspace", required=True, type=Path)
        command.add_argument("--database", required=True)
    return parser


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"understand", "fix"}:
        workspace = args.workspace.resolve()
        database = _database(workspace, args.database)
        policy = (
            TaskPolicy.code_understanding(approval_required=not args.no_approval)
            if args.command == "understand"
            else TaskPolicy.testing_fix(approval_required=not args.no_approval)
        )
        harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=policy, prompt=args.prompt)
        agent = harness.build_agent(
            model=_model(args),
            tools=_repository_tools(harness),
            max_steps=args.max_steps,
            stream_outputs=True,
            instructions=(
                "This is a code-understanding task. First use list_files, search_text, and read_file. "
                "Do not run commands. If the user requests a report file, call write_file only after the analysis is complete. "
                "If write_file returns APPROVAL_REQUIRED, stop and state that the run is waiting for human approval. "
                "Use native function calls; never emit a prose imitation of a tool call or JSON in message text. "
                "End only with the final_answer function."
                if args.command == "understand"
                else "You are repairing a repository through native function calls. Never emit prose that imitates a tool call, "
                "never output JSON for a tool call in message text, and never describe an action instead of calling its function. "
                "For every turn, call exactly one available function: list_files, search_text, read_file, write_file, run_test, "
                "or final_answer. Start by inspecting the repository with list_files/search_text/read_file. "
                "For a defect, inspect the repository's test configuration before any test command: read package.json for a Node/TypeScript project, "
                "or pyproject.toml/pytest.ini for a Python project, and use its declared runner and script. "
                "For Node/TypeScript projects, pass the directory containing package.json as run_test.cwd, then use the package script from that directory; "
                "for example argv=[\"npm\", \"test\"], cwd=\"demo-app\". Do not use --prefix or guess module-resolution paths when cwd can select the project root. "
                "A .ts/.tsx test is not a pytest target unless repository configuration explicitly says so. "
                "Run a focused test first to obtain a baseline, read the failing test and relevant source, "
                "write only the required source fix, then run the focused test again. "
                "If run_test returns failure_kind=TEST_COMMAND_INVALID, do not retry any test command until you inspect package.json and choose argv plus cwd from its configuration. "
                "If it returns failure_kind=TEST_FAILED, treat that as a valid red baseline and investigate the source/test. "
                "Run the requested final test command after the last change. "
                "If write_file or run_test returns APPROVAL_REQUIRED, stop tool use and state that the run is waiting for human approval. "
                "Call final_answer only after the final verification succeeds; otherwise use another repository tool."
            ),
        )
        answer = agent.run(args.prompt)
        state = harness.state.get_run(harness.run_id)
        if state["status"] == "stopped":
            _emit(
                {
                    "run_id": harness.run_id,
                    "database": str(database),
                    "status": "stopped",
                    "stop_reason": state["stop_reason"],
                    "answer": str(answer),
                    "acceptance": state["result"],
                }
            )
            return 1
        if state["status"] == "waiting_approval":
            pending = next(item for item in state["approvals"] if item["approval_id"] == state["pending_approval"])
            _emit(
                {
                    "run_id": harness.run_id,
                    "database": str(database),
                    "status": "waiting_approval",
                    "approval": pending,
                    "answer": str(answer),
                }
            )
            return 0
        _emit({"run_id": harness.run_id, "database": str(database), "acceptance": harness.finalize(str(answer))})
        return 0

    if args.command in {"approve", "resume"}:
        # approval_id is the primary authority. Resolve its owning Run server-side;
        # the caller cannot redirect a valid approval to another run/workspace.
        from .state_store import StateStore

        state_store = StateStore(args.database)
        found = state_store.find_approval(args.approval_id)
        if found is None:
            raise KeyError(f"Unknown approval: {args.approval_id}")
        run_id, approval = found
        saved_workspace = Path(state_store.get_run(run_id)["workspace"]).resolve()
        if args.run_id is not None and args.run_id != run_id:
            raise ValueError("APPROVAL_RUN_MISMATCH")
        if args.workspace is not None and args.workspace.resolve() != saved_workspace:
            raise ValueError("APPROVAL_WORKSPACE_MISMATCH")
        harness = GuardedCodingHarness.from_checkpoint(args.database, run_id, workspace=saved_workspace)
        if args.command == "approve":
            approved = harness.approve(args.approval_id, actor=args.actor)
            _emit({"approval_id": args.approval_id, "run_id": run_id, "approved": approved})
        else:
            _repository_tools(harness)
            executed = harness.resume_approval(args.approval_id)
            _emit({"approval_id": args.approval_id, "run_id": run_id, "executed": executed})
        return 0

    harness = GuardedCodingHarness.from_checkpoint(args.database, args.run_id, workspace=args.workspace)
    if args.command == "show":
        _emit(harness.state.get_run(args.run_id))
    elif args.command == "audit":
        _emit(harness.audit.for_run(args.run_id))
    elif args.command == "trace":
        _emit(harness.trace())
    return 0
