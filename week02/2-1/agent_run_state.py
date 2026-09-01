from dataclasses import dataclass
from typing import Any, Literal

from execution_context import ExecutionContext
from tool_messages import ToolCall


@dataclass(frozen=True)
class CompletedRun:
    """Agent 已结束并产出最终自然语言回答。"""

    answer: str
    status: Literal["completed"] = "completed"


@dataclass(frozen=True)
class WaitingForApproval:
    """Agent 已挂起；确认后必须复用 pending_call 恢复。"""

    pending_call: ToolCall
    messages: list[dict[str, Any]]
    context: ExecutionContext
    next_step: int
    approval_prompt: str
    status: Literal["waiting_for_approval"] = "waiting_for_approval"
