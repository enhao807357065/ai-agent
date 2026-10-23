from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class ReleaseState(TypedDict):
    ticket_id: str
    service: str
    version: str
    patch_diff: str
    checks_passed: int
    stages_completed: NotRequired[list[str]]
    outcome: NotRequired[str]
    report_path: NotRequired[str]
    change_level: NotRequired[Literal["L", "M", "H"]]
    approver_count: NotRequired[int]
