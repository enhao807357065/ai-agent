from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from state import Approval, CheckResult, ReleaseState


def intake(state: ReleaseState) -> dict[str, list[AIMessage]]:
    calls = [
        ("build_service", {"service": state["service"], "version": state["version"]}),
        ("run_unit_tests", {"service": state["service"]}),
        ("security_scan", {"diff": state["patch_diff"]}),
        ("dependency_audit", {"service": state["service"]}),
    ]
    tool_calls = [
        {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}
        for name, args in calls
    ]
    return {"messages": [AIMessage(content="Run release checks", tool_calls=tool_calls)]}


def collect_checks(state: ReleaseState) -> dict[str, list[CheckResult] | int]:
    checks = [
        CheckResult(**json.loads(message.content))
        for message in state.get("messages", [])
        if isinstance(message, ToolMessage)
    ]
    return {"checks": checks, "checks_passed": sum(check["passed"] for check in checks)}


def classify_change(state: ReleaseState) -> dict[str, Literal["L", "M", "H"]]:
    diff = state["patch_diff"].lower()
    if any(term in diff for term in ("auth", "payment", "password", "token")):
        return {"change_level": "H"}
    if len(diff) > 800:
        return {"change_level": "M"}
    return {"change_level": "L"}


def _as_approval(
    decision: dict[str, Any], interrupt_id: str, role: str
) -> Approval:
    return {
        "interrupt_id": interrupt_id,
        "role": role,
        "action": decision["action"],
        "actor": decision["actor"],
        "feedback": decision["feedback"],
    }


def approval_auto_L(state: ReleaseState) -> dict[str, int]:
    return {"approver_count": 0}


def approval_m(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-1", "role": "tech_lead"})
    return {"approvals": [_as_approval(decision, "INT-1", "tech_lead")]}


def approval_h_lead(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-2A", "role": "tech_lead"})
    return {"approvals": [_as_approval(decision, "INT-2A", "tech_lead")]}


def approval_h_security(state: ReleaseState) -> dict[str, list[Approval]]:
    decision = interrupt({"interrupt_id": "INT-2B", "role": "security"})
    return {"approvals": [_as_approval(decision, "INT-2B", "security")]}


def deploy_staging(state: ReleaseState) -> dict[str, list[str]]:
    return {"stages_completed": [*state.get("stages_completed", []), "staging"]}


def smoke_test(state: ReleaseState) -> dict[str, list[str]]:
    return {"stages_completed": [*state.get("stages_completed", []), "smoke"]}


def canary_10(state: ReleaseState) -> dict[str, list[str]]:
    return {"stages_completed": [*state.get("stages_completed", []), "canary_10"]}


def canary_50(state: ReleaseState) -> dict[str, list[str]]:
    return {"stages_completed": [*state.get("stages_completed", []), "canary_50"]}


def canary_100(state: ReleaseState) -> dict[str, list[str]]:
    return {"stages_completed": [*state.get("stages_completed", []), "canary_100"]}


def finalize(state: ReleaseState) -> dict[str, str]:
    outcome = "rejected" if any(item["action"] == "reject" for item in state.get("approvals", [])) else "released"
    report_path = Path("reports") / f'{state["ticket_id"]}.json'
    result = {"outcome": outcome, "report_path": str(report_path)}
    if outcome == "rejected" and "stages_completed" not in state:
        result["stages_completed"] = []
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps({**state, **result}, ensure_ascii=False, default=str), encoding="utf-8")
    return result
