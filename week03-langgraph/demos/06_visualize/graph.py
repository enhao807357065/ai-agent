from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from nodes import (
    approval_auto_L,
    approval_h_lead,
    approval_h_security,
    approval_m,
    auto_rollback,
    begin_revision,
    canary_step,
    classify_change,
    collect_checks,
    deploy_staging,
    finalize,
    intake,
    monitor_window,
)
from state import ReleaseState
from tools import CHECK_TOOLS


def route_by_level(state: ReleaseState) -> Literal["L", "M", "H"]:
    return state["change_level"]


def route_approval(state: ReleaseState) -> Literal["deploy", "reject", "revise", "stop"]:
    action = state["approvals"][-1]["action"]
    if action == "approve":
        return "deploy"
    if action in {"reject", "abort"}:
        return "reject"
    if state["revision_count"] >= state["max_revisions"]:
        return "stop"
    return "revise"


def route_after_canary(state: ReleaseState) -> Literal["monitor", "finalize"]:
    return "finalize" if state.get("outcome") == "rejected" else "monitor"


def route_after_monitor(state: ReleaseState) -> Literal["advance", "unhealthy", "released"]:
    if state["current_stage"]["status"] == "unhealthy":
        return "unhealthy"
    return "released" if state["canary_cursor"] == 3 else "advance"


def route_after_rollback(state: ReleaseState) -> Literal["retry", "abort"]:
    return "abort" if state.get("outcome") == "rolled_back" else "retry"


def build_graph(checkpointer):
    workflow = StateGraph(ReleaseState)
    workflow.add_node("intake", intake)
    workflow.add_node("tool_checks", ToolNode(CHECK_TOOLS))
    workflow.add_node("collect_checks", collect_checks)
    workflow.add_node("classify_change", classify_change)
    workflow.add_node("approval_auto_L", approval_auto_L)
    workflow.add_node("approval_m", approval_m)
    workflow.add_node("approval_h_lead", approval_h_lead)
    workflow.add_node("approval_h_security", approval_h_security)
    workflow.add_node("begin_revision", begin_revision)
    workflow.add_node("deploy_staging", deploy_staging)
    workflow.add_node("canary_step", canary_step)
    workflow.add_node("monitor_window", monitor_window)
    workflow.add_node("auto_rollback", auto_rollback)
    workflow.add_node("finalize", finalize)

    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "tool_checks")
    workflow.add_edge("tool_checks", "collect_checks")
    workflow.add_edge("collect_checks", "classify_change")
    workflow.add_conditional_edges("classify_change", route_by_level, {"L": "approval_auto_L", "M": "approval_m", "H": "approval_h_lead"})
    workflow.add_conditional_edges("approval_auto_L", route_approval, {"deploy": "deploy_staging", "reject": "finalize", "revise": "begin_revision", "stop": "finalize"})
    workflow.add_conditional_edges("approval_m", route_approval, {"deploy": "deploy_staging", "reject": "finalize", "revise": "begin_revision", "stop": "finalize"})
    workflow.add_conditional_edges("approval_h_lead", route_approval, {"deploy": "approval_h_security", "reject": "finalize", "revise": "begin_revision", "stop": "finalize"})
    workflow.add_conditional_edges("approval_h_security", route_approval, {"deploy": "deploy_staging", "reject": "finalize", "revise": "begin_revision", "stop": "finalize"})
    workflow.add_edge("begin_revision", "intake")
    workflow.add_edge("deploy_staging", "canary_step")
    workflow.add_conditional_edges("canary_step", route_after_canary, {"monitor": "monitor_window", "finalize": "finalize"})
    workflow.add_conditional_edges("monitor_window", route_after_monitor, {"advance": "canary_step", "unhealthy": "auto_rollback", "released": "finalize"})
    workflow.add_conditional_edges("auto_rollback", route_after_rollback, {"retry": "canary_step", "abort": "finalize"})
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=checkpointer)
