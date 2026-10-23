from __future__ import annotations

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

CheckResult = TypedDict("CheckResult", {"name": str, "passed": bool})


class Approval(TypedDict):
    interrupt_id: Literal["INT-1", "INT-2A", "INT-2B"]
    role: Literal["tech_lead", "security"]
    action: Literal["approve", "reject"]
    actor: str
    feedback: str


class ReleaseState(TypedDict):
    ticket_id: str
    service: str
    version: str
    patch_diff: str
    checks_passed: int
    stages_completed: NotRequired[list[str]]
    outcome: NotRequired[Literal["released", "rejected"]]
    report_path: NotRequired[str]
    change_level: NotRequired[Literal["L", "M", "H"]]
    approver_count: NotRequired[int]
    messages: NotRequired[Annotated[list[AnyMessage], add_messages]]
    checks: NotRequired[list[CheckResult]]
    approvals: NotRequired[Annotated[list[Approval], list.__add__]]
