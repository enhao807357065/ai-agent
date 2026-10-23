from __future__ import annotations

from pathlib import Path
import sys

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parents[1]))

from checkpointer import open_checkpointer
from graph import build_graph


def payload(action: str, actor: str = "reviewer") -> dict[str, str]:
    return {"action": action, "actor": actor, "feedback": "reviewed"}


def input_state(diff: str) -> dict[str, str]:
    return {"ticket_id": "REL-001", "service": "orders", "version": "1.0.0", "patch_diff": diff}


def test_m_level_pauses_at_tech_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "m-pause"}}
        graph.invoke(input_state("x" * 1200), config)
        snapshot = graph.get_state(config)
    assert snapshot.next == ("approval_m",)
    assert snapshot.tasks[0].interrupts[0].value["interrupt_id"] == "INT-1"


def test_m_approval_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "m-approve"}}
        graph.invoke(input_state("x" * 1200), config)
        result = graph.invoke(Command(resume=payload("approve")), config)
    assert result["outcome"] == "released"
    assert [item["role"] for item in result["approvals"]] == ["tech_lead"]


def test_h_requires_two_serial_approvals(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "h-approve"}}
        graph.invoke(input_state("auth token"), config)
        assert graph.get_state(config).tasks[0].interrupts[0].value["interrupt_id"] == "INT-2A"
        graph.invoke(Command(resume=payload("approve")), config)
        assert graph.get_state(config).tasks[0].interrupts[0].value["interrupt_id"] == "INT-2B"
        result = graph.invoke(Command(resume=payload("approve")), config)
    assert result["outcome"] == "released"
    assert len(result["approvals"]) == 2


def test_sqlite_restores_pending_approval_across_contexts(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    config = {"configurable": {"thread_id": "cross-process"}}
    with open_checkpointer() as saver:
        build_graph(saver).invoke(input_state("x" * 1200), config)
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        snapshot = graph.get_state(config)
        result = graph.invoke(Command(resume=payload("approve")), config)
    assert snapshot.values["ticket_id"] == "REL-001"
    assert snapshot.next == ("approval_m",)
    assert result["outcome"] == "released"


def test_reject_short_circuits_before_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "m-reject"}}
        graph.invoke(input_state("x" * 1200), config)
        graph.invoke(Command(resume=payload("reject")), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["outcome"] == "rejected"
    assert snapshot.values["stages_completed"] == []
