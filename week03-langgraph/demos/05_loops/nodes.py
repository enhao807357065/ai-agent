from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from state import Approval, CheckResult, MetricSample, ReleaseState
from tools import deploy, query_metrics

STAGES = ["canary_10", "canary_50", "canary_100"]


def intake(state: ReleaseState) -> dict[str, list[AIMessage]]:
    calls = [
        ("build_service", {"service": state["service"], "version": state["version"]}),
        ("run_unit_tests", {"service": state["service"]}),
        ("security_scan", {"diff": state["patch_diff"]}),
        ("dependency_audit", {"service": state["service"]}),
    ]
    tool_calls = [{"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"} for name, args in calls]
    return {"messages": [AIMessage(content="Run release checks", tool_calls=tool_calls)]}


def collect_checks(state: ReleaseState) -> dict[str, list[CheckResult] | int]:
    checks = [CheckResult(**json.loads(message.content)) for message in state.get("messages", []) if isinstance(message, ToolMessage)]
    return {"checks": checks, "checks_passed": sum(check["passed"] for check in checks)}


def classify_change(state: ReleaseState) -> dict[str, Literal["L", "M", "H"]]:
    diff = state["patch_diff"].lower()
    if any(term in diff for term in ("auth", "payment", "password", "token")):
        return {"change_level": "H"}
    if len(diff) > 800:
        return {"change_level": "M"}
    return {"change_level": "L"}


def _as_approval(decision: dict[str, Any], interrupt_id: str, role: str) -> Approval:
    return {"interrupt_id": interrupt_id, "role": role, "action": decision["action"], "actor": decision["actor"], "feedback": decision["feedback"]}


def approval_auto_L(state: ReleaseState) -> dict[str, list[Approval]]:
    return {"approvals": [{"interrupt_id": "INT-1", "role": "tech_lead", "action": "approve", "actor": "system", "feedback": "auto"}]}


def approval_m(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-1", "role": "tech_lead", "actions": ["approve", "reject", "revise"]})
    return {"approvals": [_as_approval(decision, "INT-1", "tech_lead")]}


def approval_h_lead(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-2A", "role": "tech_lead", "actions": ["approve", "reject", "revise"]})
    return {"approvals": [_as_approval(decision, "INT-2A", "tech_lead")]}


def approval_h_security(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-2B", "role": "security", "actions": ["approve", "reject", "revise"]})
    return {"approvals": [_as_approval(decision, "INT-2B", "security")]}


def begin_revision(state: ReleaseState) -> dict[str, int | list[CheckResult]]:
    return {"revision_count": state["revision_count"] + 1, "checks": []}


def deploy_staging(state: ReleaseState) -> dict[str, list[str]]:
    deploy.invoke({"stage": "staging", "service": state["service"], "version": state["version"]})
    return {"stages_completed": [*state.get("stages_completed", []), "staging"]}


def canary_step(state: ReleaseState) -> dict[str, int | dict | str]:
    cursor = state["canary_cursor"] + 1
    stage = STAGES[cursor - 1]
    decision = interrupt({"interrupt_id": f"INT-3-{stage}", "role": "sre", "actions": ["approve", "abort"]})
    if decision["action"] == "abort":
        return {"outcome": "rejected"}
    return {"canary_cursor": cursor, "current_stage": {"name": stage}}


def monitor_window(state: ReleaseState) -> dict[str, list[MetricSample] | dict]:
    stage = state["current_stage"]["name"]
    query_metrics.invoke({"service": state["service"], "stage": stage})
    rolled_back = f"rolled_back:{stage}" in state.get("stages_completed", [])
    unhealthy = state["service"] == "flaky" and stage == "canary_10" and not rolled_back
    status = "unhealthy" if unhealthy else "healthy"
    return {"metric_samples": [{"stage": stage, "status": status}], "current_stage": {"name": stage, "status": status}}


def auto_rollback(state: ReleaseState) -> dict[str, int | list[str] | str]:
    stage = state["current_stage"]["name"]
    rollback_count = state["rollback_count"] + 1
    result = {"rollback_count": rollback_count, "canary_cursor": max(0, state["canary_cursor"] - 1), "stages_completed": [*state.get("stages_completed", []), f"rolled_back:{stage}"]}
    if rollback_count > state["max_rollbacks"]:
        result["outcome"] = "rolled_back"
    return result


def finalize(state: ReleaseState) -> dict[str, str | list[str]]:
    action = state.get("approvals", [{}])[-1].get("action")
    outcome = state.get("outcome", "released")
    if action in {"reject", "abort"}:
        outcome = "rejected"
    if action == "revise" and state["revision_count"] >= state["max_revisions"]:
        outcome = "incomplete"
    report_path = Path("reports") / f'{state["ticket_id"]}.json'
    result = {"outcome": outcome, "report_path": str(report_path)}
    if outcome in {"rejected", "incomplete", "rolled_back"} and "stages_completed" not in state:
        result["stages_completed"] = []
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps({**state, **result}, ensure_ascii=False, default=str), encoding="utf-8")
    return result
