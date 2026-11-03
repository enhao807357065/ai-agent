"""Run guarded repository tasks with smolagents ToolCallingAgent.

Examples:
  python examples/guarded_coding_demo.py understand --workspace ./repo --prompt '解释 src/app.py'
  python examples/guarded_coding_demo.py fix --workspace ./repo --prompt '修复失败测试' --no-approval
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from smolagents import OpenAIModel, tool
from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded smolagents repository task")
    parser.add_argument("task", choices=("understand", "fix"))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--no-approval", action="store_true", help="Allow write/execute without human approval; only for local learning.")
    return parser.parse_args()


def main() -> None:
    args = arguments()
    workspace = args.workspace.resolve()
    database = (args.database or workspace / ".guarded-harness.sqlite").resolve()
    policy = (
        TaskPolicy.code_understanding()
        if args.task == "understand"
        else TaskPolicy.testing_fix(approval_required=not args.no_approval)
    )
    harness = GuardedCodingHarness.create(workspace=workspace, database=database, task=policy, prompt=args.prompt)

    @tool
    def read_file(path: str) -> str:
        """Read one workspace-relative text file.

        Args:
            path: Path relative to the repository workspace.
        """
        return harness.sandbox.read_text(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write one workspace-relative text file.

        Args:
            path: Path relative to the repository workspace.
            content: Complete replacement content for the file.
        """
        harness.sandbox.write_text(path, content)
        return f"wrote {path}"

    @tool
    def run_test(argv: list[str]) -> dict:
        """Run one allowlisted repository command.

        Args:
            argv: Command argv. First item must be python, pytest, or git.
        """
        return harness.sandbox.run_command(argv)

    # Register server-owned handlers with the right risks before exposing them
    # to the model. build_agent will see names already registered and cannot
    # downgrade the write / execute classification.
    harness.register_tool("read_file", {"path": str}, read_file, risk="read")
    harness.register_tool("write_file", {"path": str, "content": str}, write_file, risk="write")
    harness.register_tool("run_test", {"argv": list}, run_test, risk="execute")

    api_key = os.environ.get("GATEWAY_API_KEY")
    if not api_key:
        raise RuntimeError("Set GATEWAY_API_KEY in the shell; do not place it in source code.")
    model = OpenAIModel(
        model_id=os.environ.get("GATEWAY_MODEL_ID", "chat-default"),
        api_base=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=api_key,
        temperature=0,
    )
    agent = harness.build_agent(model=model, tools=[read_file, write_file, run_test], max_steps=12, stream_outputs=True)
    answer = agent.run(args.prompt)
    acceptance = harness.finalize(str(answer))
    print(f"\nrun_id={harness.run_id}")
    print(f"database={database}")
    print(f"acceptance={acceptance}")


if __name__ == "__main__":
    main()
