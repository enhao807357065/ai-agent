from __future__ import annotations

import json
from pathlib import Path
import sys

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parents[1]))

from checkpointer import open_checkpointer
from graph import build_graph
from visualize import export_mermaid, mermaid_text, stream_nodes


def initial() -> dict:
    return {"ticket_id": "REL-006", "service": "orders", "version": "1.0.0", "patch_diff": "x" * 1200, "revision_count": 0, "max_revisions": 1, "rollback_count": 0, "max_rollbacks": 2, "canary_cursor": 0}


def test_mermaid_contains_release_graph_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        text = mermaid_text(build_graph(saver))
    assert all(item in text for item in ("graph TD", "intake", "tool_checks", "canary_step", "auto_rollback", "begin_revision", "finalize"))


def test_export_mermaid_keeps_mmd_and_reports_png_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        info = export_mermaid(build_graph(saver), tmp_path / "export")
    assert Path(info["mmd_path"]).exists()
    assert "png_status" in info


def test_stream_nodes_shows_m_pause_without_approval_node(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    with open_checkpointer() as saver:
        nodes = stream_nodes(build_graph(saver), initial(), {"configurable": {"thread_id": "watch-m"}})
    assert nodes[:4] == ["intake", "tool_checks", "collect_checks", "classify_change"]
    assert nodes[-1] == "⏸ INT-1"
    assert "approval_m" not in nodes


def test_checkpoint_pause_and_resume_reaches_sre_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    config = {"configurable": {"thread_id": "resume-m"}}
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        graph.invoke(initial(), config)
        snapshot = graph.get_state(config)
        graph.invoke(Command(resume={"action": "approve", "actor": "lead", "feedback": "ok"}), config)
        resumed = graph.get_state(config)
    assert snapshot.next == ("approval_m",)
    assert snapshot.tasks[0].interrupts[0].value["interrupt_id"] == "INT-1"
    assert resumed.tasks[0].interrupts[0].value["interrupt_id"] == "INT-3-canary_10"


def test_studio_graph_and_langgraph_config_load():
    import studio_graph

    config = json.loads(Path(__file__).parents[1].joinpath("langgraph.json").read_text())
    assert studio_graph.graph is not None
    assert config["graphs"]["release_06"] == "./studio_graph.py:graph"
