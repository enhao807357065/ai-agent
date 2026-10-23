from __future__ import annotations

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

CheckResult = TypedDict("CheckResult", {"name": str, "passed": bool})


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
    messages: NotRequired[Annotated[list[AnyMessage], add_messages]]
    checks: NotRequired[list[CheckResult]]
