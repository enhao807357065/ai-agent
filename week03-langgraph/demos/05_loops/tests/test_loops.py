from __future__ import annotations

from pathlib import Path
import sys

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parents[1]))

from checkpointer import open_checkpointer
from graph import build_graph


def release_input(*, service: str = "orders", max_revisions: int = 1, max_rollbacks: int = 2):
    return {
        "ticket_id": "REL-001",
        "service": service,
        "version": "1.0.0",
        "patch_diff": "x" * 1200,
        "revision_count": 0,
        "max_revisions": max_revisions,
        "rollback_count": 0,
        "max_rollbacks": max_rollbacks,
        "canary_cursor": 0,
    }


def decide(action: str, actor: str) -> Command:
    return Command(resume={"action": action, "actor": actor, "feedback": "reviewed"})


def graph_and_config(tmp_path, monkeypatch, thread_id: str):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    return {"configurable": {"thread_id": thread_id}}


def test_revise_rechecks_and_pauses_at_m_approval(tmp_path, monkeypatch):
    config = graph_and_config(tmp_path, monkeypatch, "revise-once")
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(release_input(), config)
        graph.invoke(decide("revise", "lead"), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["revision_count"] == 1
    assert snapshot.next == ("approval_m",)


def test_revise_approve_and_three_sre_decisions_releases(tmp_path, monkeypatch):
    config = graph_and_config(tmp_path, monkeypatch, "revise-release")
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(release_input(), config)
        graph.invoke(decide("revise", "lead"), config)
        graph.invoke(decide("approve", "lead"), config)
        for index in range(3):
            graph.invoke(decide("approve", f"sre-{index}"), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["revision_count"] == 1
    assert snapshot.values["outcome"] == "released"


def test_revision_budget_stops_on_second_revise(tmp_path, monkeypatch):
    config = graph_and_config(tmp_path, monkeypatch, "revise-budget")
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(release_input(max_revisions=1), config)
        graph.invoke(decide("revise", "lead"), config)
        graph.invoke(decide("revise", "lead"), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["revision_count"] == 1
    assert snapshot.next == ()
    assert snapshot.values["outcome"] == "incomplete"


def test_flaky_service_rolls_back_once_then_releases(tmp_path, monkeypatch):
    config = graph_and_config(tmp_path, monkeypatch, "rollback-release")
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(release_input(service="flaky", max_rollbacks=2), config)
        graph.invoke(decide("approve", "lead"), config)
        for index in range(4):
            graph.invoke(decide("approve", f"sre-{index}"), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["rollback_count"] == 1
    assert snapshot.values["outcome"] == "released"
    assert any(item.startswith("rolled_back:") for item in snapshot.values["stages_completed"])


def test_rollback_budget_ends_as_rolled_back(tmp_path, monkeypatch):
    config = graph_and_config(tmp_path, monkeypatch, "rollback-budget")
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(release_input(service="flaky", max_rollbacks=0), config)
        graph.invoke(decide("approve", "lead"), config)
        graph.invoke(decide("approve", "sre"), config)
        snapshot = graph.get_state(config)
    assert snapshot.values["rollback_count"] == 1
    assert snapshot.values["outcome"] == "rolled_back"
    assert snapshot.next == ()
