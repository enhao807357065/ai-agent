from __future__ import annotations

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

CheckResult = TypedDict("CheckResult", {"name": str, "passed": bool})
MetricSample = TypedDict("MetricSample", {"stage": str, "status": Literal["healthy", "unhealthy"]})


class Approval(TypedDict):
    interrupt_id: Literal["INT-1", "INT-2A", "INT-2B", "INT-3-canary_10", "INT-3-canary_50", "INT-3-canary_100"]
    role: Literal["tech_lead", "security", "sre"]
    action: Literal["approve", "reject", "revise", "abort"]
    actor: str
    feedback: str


class ReleaseState(TypedDict):
    ticket_id: str
    service: str
    version: str
    patch_diff: str
    revision_count: int
    max_revisions: int
    rollback_count: int
    max_rollbacks: int
    canary_cursor: int
    checks_passed: NotRequired[int]
    stages_completed: NotRequired[list[str]]
    outcome: NotRequired[Literal["released", "rejected", "rolled_back", "incomplete"]]
    report_path: NotRequired[str]
    change_level: NotRequired[Literal["L", "M", "H"]]
    messages: NotRequired[Annotated[list[AnyMessage], add_messages]]
    checks: NotRequired[list[CheckResult]]
    approvals: NotRequired[Annotated[list[Approval], list.__add__]]
    current_stage: NotRequired[dict]
    metric_samples: NotRequired[Annotated[list[MetricSample], list.__add__]]
