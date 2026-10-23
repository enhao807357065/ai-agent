from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from nodes import (
    approval_auto_L,
    approval_h_lead,
    approval_h_security,
    approval_m,
    canary_10,
    canary_50,
    canary_100,
    classify_change,
    collect_checks,
    deploy_staging,
    finalize,
    intake,
    smoke_test,
)
from state import ReleaseState
from tools import CHECK_TOOLS


def route_by_level(state: ReleaseState) -> Literal["L", "M", "H"]:
    return state["change_level"]


def route_after_approval(state: ReleaseState) -> Literal["approve", "reject"]:
    return "reject" if any(item["action"] == "reject" for item in state.get("approvals", [])) else "approve"


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
    workflow.add_node("deploy_staging", deploy_staging)
    workflow.add_node("smoke_test", smoke_test)
    workflow.add_node("canary_10", canary_10)
    workflow.add_node("canary_50", canary_50)
    workflow.add_node("canary_100", canary_100)
    workflow.add_node("finalize", finalize)
    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "tool_checks")
    workflow.add_edge("tool_checks", "collect_checks")
    workflow.add_edge("collect_checks", "classify_change")
    workflow.add_conditional_edges(
        "classify_change",
        route_by_level,
        {"L": "approval_auto_L", "M": "approval_m", "H": "approval_h_lead"},
    )
    workflow.add_edge("approval_auto_L", "deploy_staging")
    workflow.add_conditional_edges("approval_m", route_after_approval, {"approve": "deploy_staging", "reject": "finalize"})
    workflow.add_conditional_edges("approval_h_lead", route_after_approval, {"approve": "approval_h_security", "reject": "finalize"})
    workflow.add_conditional_edges("approval_h_security", route_after_approval, {"approve": "deploy_staging", "reject": "finalize"})
    workflow.add_edge("deploy_staging", "smoke_test")
    workflow.add_edge("smoke_test", "canary_10")
    workflow.add_edge("canary_10", "canary_50")
    workflow.add_edge("canary_50", "canary_100")
    workflow.add_edge("canary_100", "finalize")
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=checkpointer)
