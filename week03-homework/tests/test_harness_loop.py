"""smolagents ToolCallingAgent loop integration through GuardedCodingHarness."""

from __future__ import annotations

from pathlib import Path

from smolagents import tool
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole, Model

from smolagents.guarded_harness.harness import GuardedCodingHarness, TaskPolicy


class TwoTurnModel(Model):
    model_id = "fake-tool-model"

    def generate(self, messages, tools_to_call_from=None, stop_sequences=None):
        if len(messages) < 3:
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="read source",
                tool_calls=[ChatMessageToolCall(
                    id="provider-read-1",
                    type="function",
                    function=ChatMessageToolCallFunction(name="read_repo", arguments={"path": "src.py"}),
                )],
            )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="finish",
            tool_calls=[ChatMessageToolCall(
                id="provider-final-1",
                type="function",
                function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "understood"}),
            )],
        )


def test_guarded_agent_routes_provider_tool_call_id_through_gate(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src.py").write_text("def target():\n    return 1\n")
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(),
    )

    @tool
    def read_repo(path: str) -> str:
        """Read a repository file.

        Args:
            path: A workspace-relative file path.
        """
        return harness.sandbox.read_text(path)

    agent = harness.build_agent(model=TwoTurnModel(), tools=[read_repo], max_steps=3, verbosity_level=0)
    result = agent.run("understand target")

    assert result == "understood"
    assert [event["event"] for event in harness.audit.for_tool_call("provider-read-1")] == [
        "received", "allowed", "executing", "result"
    ]


class FinalOnlyModel(Model):
    model_id = "fake-final-model"

    def generate(self, messages, tools_to_call_from=None, stop_sequences=None):
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="done",
            tool_calls=[ChatMessageToolCall(
                id="provider-final-without-artifact",
                type="function",
                function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "done"}),
            )],
        )


class NeverFinalModel(Model):
    model_id = "fake-never-final-model"

    def generate(self, messages, tools_to_call_from=None, stop_sequences=None):
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="still thinking",
            tool_calls=[ChatMessageToolCall(
                id=f"provider-read-{len(messages)}",
                type="function",
                function=ChatMessageToolCallFunction(name="read_repo", arguments={"path": "src.py"}),
            )],
        )


def test_guarded_agent_rejects_model_final_answer_without_requested_deliverable(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
        prompt="Write artifacts/login-report.md",
    )
    agent = harness.build_agent(model=FinalOnlyModel(), tools=[], max_steps=1, verbosity_level=0)

    agent.run("write the report")

    state = harness.state.get_run(harness.run_id)
    assert state["status"] == "failed"
    assert state["stop_reason"] == "FINAL_ACCEPTANCE_FAILED"
    assert any(
        event["event"] == "rejected" and event["rule"] == "FINAL_ACCEPTANCE_FAILED"
        for event in harness.audit.for_tool_call("provider-final-without-artifact")
    )



class RepeatedActionModel(Model):
    model_id = "fake-repeated-action-model"

    def generate(self, messages, tools_to_call_from=None, stop_sequences=None):
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="read again",
            tool_calls=[ChatMessageToolCall(
                id=f"repeat-{len(messages)}",
                type="function",
                function=ChatMessageToolCallFunction(name="read_repo", arguments={"path": "src.py"}),
            )],
        )


def test_guarded_agent_stops_loop_on_repeated_tool_action(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src.py").write_text("x = 1\n")
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
    )

    @tool
    def read_repo(path: str) -> str:
        """Read one repository file.

        Args:
            path: workspace-relative path.
        """
        return harness.sandbox.read_text(path)

    agent = harness.build_agent(model=RepeatedActionModel(), tools=[read_repo], max_steps=30, verbosity_level=0)
    result = agent.run("read the same source repeatedly")

    state = harness.state.get_run(harness.run_id)
    assert result["code"] == "REPEATED_TOOL_ACTION"
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "REPEATED_TOOL_ACTION"
    assert len(state["completed_tool_calls"]) == 2


def test_guarded_agent_max_steps_sets_stop_reason_without_claiming_success(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src.py").write_text("x = 1\n")
    harness = GuardedCodingHarness.create(
        workspace=workspace,
        database=tmp_path / "harness.sqlite",
        task=TaskPolicy.code_understanding(approval_required=False),
    )

    @tool
    def read_repo(path: str) -> str:
        """Read a repository file.

        Args:
            path: workspace-relative path.
        """
        return harness.sandbox.read_text(path)

    agent = harness.build_agent(model=NeverFinalModel(), tools=[read_repo], max_steps=1, verbosity_level=0)
    agent.run("keep going")

    state = harness.state.get_run(harness.run_id)
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "MAX_STEPS_REACHED"
