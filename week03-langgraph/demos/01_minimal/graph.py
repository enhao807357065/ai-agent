from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from nodes import (
    canary_10,
    canary_50,
    canary_100,
    deploy_staging,
    finalize,
    intake,
    smoke_test,
)
from state import ReleaseState


def build_graph():
    workflow = StateGraph(ReleaseState)
    workflow.add_node("intake", intake)
    workflow.add_node("deploy_staging", deploy_staging)
    workflow.add_node("smoke_test", smoke_test)
    workflow.add_node("canary_10", canary_10)
    workflow.add_node("canary_50", canary_50)
    workflow.add_node("canary_100", canary_100)
    workflow.add_node("finalize", finalize)
    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "deploy_staging")
    workflow.add_edge("deploy_staging", "smoke_test")
    workflow.add_edge("smoke_test", "canary_10")
    workflow.add_edge("canary_10", "canary_50")
    workflow.add_edge("canary_50", "canary_100")
    workflow.add_edge("canary_100", "finalize")
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=InMemorySaver())
