from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from state import ReleaseState


def intake(state: ReleaseState) -> dict[str, int]:
    checks = [lambda: True, lambda: True, lambda: True, lambda: True]
    return {"checks_passed": sum(check() for check in checks)}


def classify_change(state: ReleaseState) -> dict[str, Literal["L", "M", "H"]]:
    diff = state["patch_diff"].lower()
    if any(term in diff for term in ("auth", "payment", "password", "token")):
        return {"change_level": "H"}
    if len(diff) > 800:
        return {"change_level": "M"}
    return {"change_level": "L"}


def approval_auto_L(state: ReleaseState) -> dict[str, int]:
    return {"approver_count": 0}


def approval_lead_M(state: ReleaseState) -> dict[str, int]:
    return {"approver_count": 1}


def approval_dual_H(state: ReleaseState) -> dict[str, int]:
    return {"approver_count": 2}


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
    report_path = Path("reports") / f'{state["ticket_id"]}.json'
    result = {"outcome": "released", "report_path": str(report_path)}
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps({**state, **result}, ensure_ascii=False), encoding="utf-8")
    return result
