from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import ToolMessage

sys.path.insert(0, str(Path(__file__).parents[1]))

from graph import build_graph


async def run_release(patch_diff: str):
    states = [
        state
        async for state in build_graph().astream(
            {
                "ticket_id": "REL-001",
                "service": "orders",
                "version": "1.0.0",
                "patch_diff": patch_diff,
            },
            config={"configurable": {"thread_id": "REL-001"}},
            stream_mode="values",
        )
    ]
    return states[-1]


def test_tool_checks_collect_all_check_results():
    state = asyncio.run(run_release("fix typo"))
    assert {check["name"] for check in state["checks"]} == {
        "build_service",
        "run_unit_tests",
        "security_scan",
        "dependency_audit",
    }


def test_all_tool_checks_pass():
    state = asyncio.run(run_release("fix typo"))
    assert state["checks_passed"] == 4


def test_routing_branches_match_the_previous_demo():
    cases = [("fix typo", "L", 0), ("x" * 1200, "M", 1), ("rotate auth token", "H", 2)]
    for patch_diff, level, approver_count in cases:
        state = asyncio.run(run_release(patch_diff))
        assert state["change_level"] == level
        assert state["approver_count"] == approver_count


def test_messages_end_with_four_tool_messages():
    state = asyncio.run(run_release("fix typo"))
    assert len(state["messages"][-4:]) == 4
    assert all(isinstance(message, ToolMessage) for message in state["messages"][-4:])
