from __future__ import annotations

import json
from pathlib import Path

from state import ReleaseState


def intake(state: ReleaseState) -> dict[str, int]:
    checks = [lambda: True, lambda: True, lambda: True, lambda: True]
    return {"checks_passed": sum(check() for check in checks)}


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
