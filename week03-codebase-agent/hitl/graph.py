"""Pure LangGraph policy graph; SQLite ticket storage supplies durability.

Installed langgraph supports StateGraph, but this minimal approval graph has no
long-running execution to interrupt. Ticket waiting/restore state is persisted
by service.py, so restart safety does not depend on an in-memory checkpointer.
"""
from __future__ import annotations
from typing import Literal
from typing_extensions import TypedDict
from langgraph.graph import END, START, StateGraph

class ApprovalState(TypedDict):
    requested_by: str
    approvers: list[str]
    action: Literal["approve", "reject"]
    actor: str
    roles: list[str]
    feedback: str
    status: Literal["approved", "rejected"]
    code: str

def evaluate_approval(state: ApprovalState) -> dict[str, str]:
    if state["action"] != "approve":
        return {"status": "rejected", "code": "REJECTED", "feedback": state["feedback"] or "rejected by approver"}
    if state["actor"] == state["requested_by"]:
        return {"status": "rejected", "code": "SELF_APPROVAL_FORBIDDEN", "feedback": "self-approval is forbidden"}
    if not set(state["roles"]).intersection(state["approvers"]):
        return {"status": "rejected", "code": "APPROVER_ROLE_REQUIRED", "feedback": "actor lacks an approver role"}
    return {"status": "approved", "code": "APPROVED", "feedback": state["feedback"] or "approved"}

def build_approval_graph():
    workflow = StateGraph(ApprovalState)
    workflow.add_node("evaluate_approval", evaluate_approval)
    workflow.add_edge(START, "evaluate_approval")
    workflow.add_edge("evaluate_approval", END)
    return workflow.compile()
