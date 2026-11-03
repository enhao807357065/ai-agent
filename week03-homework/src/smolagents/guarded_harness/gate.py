"""The sole governed entry point for model-originated tool calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from .audit import AuditLog
from .state_store import StateStore


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class RegisteredTool:
    schema: dict[str, type]
    handler: Callable[..., Any]
    risk: str


class Gate:
    def __init__(
        self,
        *,
        state: StateStore,
        audit: AuditLog,
        approval_required: bool,
        allowed_risks: set[str] | None = None,
        path_validator: Callable[[str], Any] | None = None,
        checkpoint: Callable[[str], None] | None = None,
        workspace_digest: Callable[[], str] | None = None,
        completion: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.state = state
        self.audit = audit
        self.approval_required = approval_required
        self.allowed_risks = allowed_risks or {"read", "write", "execute"}
        self.path_validator = path_validator
        self.checkpoint = checkpoint
        self.workspace_digest = workspace_digest or (lambda: "")
        self.completion = completion
        self.tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, schema: dict[str, type], handler: Callable[..., Any], *, risk: str) -> None:
        self.tools[name] = RegisteredTool(schema=schema, handler=handler, risk=risk)

    @staticmethod
    def _error(call: ToolCall, code: str, message: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "tool_call_id": call.id, "code": code, "message": message, **extra}

    def _reject(self, run_id: str, call: ToolCall, code: str, message: str) -> dict[str, Any]:
        self.audit.append(
            run_id,
            "rejected",
            tool_call_id=call.id,
            rule=code,
            decision="rejected",
            actor="system",
            message=message,
        )
        return self._error(call, code, message)

    @staticmethod
    def _action_key(call: ToolCall) -> str:
        return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"

    def _count_completed_action(self, state: dict[str, Any], action_key: str) -> int:
        """Count only the immediately preceding identical completed actions.

        A repair naturally re-runs a command after reads or a write. The circuit
        breaker therefore detects an actual consecutive retry loop, not merely
        two historical uses of the same command elsewhere in the Run.
        """
        sequence = state.get("completed_call_sequence") or list(state["completed_tool_calls"])
        consecutive = 0
        for tool_call_id in reversed(sequence):
            if state["completed_tool_calls"][tool_call_id].get("_action_key") != action_key:
                break
            consecutive += 1
        return consecutive

    def _trailing_invalid_test_results(self, state: dict[str, Any]) -> int:
        """Count consecutive invalid test-command results at the execution tail."""
        sequence = state.get("completed_call_sequence") or list(state["completed_tool_calls"])
        count = 0
        for tool_call_id in reversed(sequence):
            result = state["completed_tool_calls"][tool_call_id]
            content = result.get("content")
            if not isinstance(content, dict) or content.get("failure_kind") != "TEST_COMMAND_INVALID":
                break
            count += 1
        return count

    def _checkpoint(self, run_id: str, reason: str) -> None:
        if self.checkpoint is not None:
            self.checkpoint(reason)

    def final_answer(self, run_id: str, tool_call_id: str, answer: str) -> dict[str, Any]:
        """Gate the model's completion claim through server-owned acceptance."""
        self.audit.append(run_id, "received", tool_call_id=tool_call_id, tool="final_answer")
        if self.completion is None:
            return self._reject(run_id, ToolCall(tool_call_id, "final_answer", {}), "FINAL_ANSWER_NOT_CONFIGURED", "completion gate is not configured")
        acceptance = self.completion(answer)
        if acceptance["status"] != "completed":
            self.audit.append(
                run_id,
                "rejected",
                tool_call_id=tool_call_id,
                rule="FINAL_ACCEPTANCE_FAILED",
                decision="rejected",
                actor="system",
                issues=acceptance["issues"],
            )
            return self._error(ToolCall(tool_call_id, "final_answer", {}), "FINAL_ACCEPTANCE_FAILED", "final answer lacks required evidence", acceptance=acceptance)
        self.audit.append(run_id, "result", tool_call_id=tool_call_id, ok=True, decision="accepted")
        return {"ok": True, "tool_call_id": tool_call_id, "content": answer, "acceptance": acceptance}

    def invoke(self, run_id: str, call: ToolCall, *, resume_approved: bool = False) -> dict[str, Any]:
        self.audit.append(run_id, "received", tool_call_id=call.id, tool=call.name)
        state = self.state.get_run(run_id)
        if state.get("status") == "stopped":
            reason = state.get("stop_reason") or "RUN_STOPPED"
            return self._reject(run_id, call, reason, "run has already stopped")
        action_key = self._action_key(call)
        if call.id in state["completed_tool_calls"]:
            return self._reject(run_id, call, "REPEATED_TOOL_CALL", "tool call was already completed")
        tool = self.tools.get(call.name)
        if tool is None:
            return self._reject(run_id, call, "TOOL_NOT_FOUND", "tool is not registered")
        if call.name == "run_test" and self._trailing_invalid_test_results(state) >= 2:
            self.state.set_stop(run_id, status="stopped", reason="TEST_COMMAND_CONFIGURATION_UNRESOLVED")
            self._checkpoint(run_id, "test_command_configuration_unresolved")
            return self._reject(
                run_id,
                call,
                "TEST_COMMAND_CONFIGURATION_UNRESOLVED",
                "two consecutive test commands were invalid; inspect project configuration before retrying",
            )
        if self._count_completed_action(state, action_key) >= 2:
            self.state.set_stop(run_id, status="stopped", reason="REPEATED_TOOL_ACTION")
            self._checkpoint(run_id, "repeated_tool_action")
            return self._reject(run_id, call, "REPEATED_TOOL_ACTION", "same tool and arguments completed twice consecutively")
        if state["budget"]["tool_calls"] >= state["budget"]["max_tool_calls"]:
            self.state.set_stop(run_id, status="stopped", reason="BUDGET_EXHAUSTED")
            self._checkpoint(run_id, "budget_exhausted")
            return self._reject(run_id, call, "BUDGET_EXHAUSTED", "tool-call budget is exhausted")
        if tool.risk not in self.allowed_risks:
            return self._reject(run_id, call, "RISK_NOT_ALLOWED", f"{tool.risk} actions are not allowed for this task")
        for key, expected_type in tool.schema.items():
            value = call.arguments.get(key)
            if not isinstance(value, expected_type):
                return self._reject(run_id, call, "INVALID_ARGUMENT", f"{key} must be {expected_type.__name__}")
        unexpected = set(call.arguments) - set(tool.schema)
        if unexpected:
            return self._reject(run_id, call, "INVALID_ARGUMENT", f"unexpected arguments: {', '.join(sorted(unexpected))}")
        if self.path_validator is not None and isinstance(call.arguments.get("path"), str):
            try:
                self.path_validator(call.arguments["path"])
            except Exception as exc:
                return self._reject(run_id, call, "PATH_NOT_ALLOWED", type(exc).__name__)

        if state.get("pending_approval"):
            return self._reject(run_id, call, "PENDING_APPROVAL_EXISTS", "another action is waiting for human approval")
        if tool.risk in {"write", "execute"} and self.approval_required and not resume_approved:
            approval_id = uuid4().hex
            approval = {
                "approval_id": approval_id,
                "tool_call_id": call.id,
                "tool": call.name,
                "arguments": call.arguments,
                "risk": tool.risk,
                "status": "pending",
            }
            self.state.add_approval(run_id, approval)
            self.audit.append(run_id, "approval_requested", tool_call_id=call.id, approval_id=approval_id, risk=tool.risk)
            self._checkpoint(run_id, "approval_requested")
            return self._error(call, "APPROVAL_REQUIRED", "human approval is required", approval_id=approval_id)

        self.audit.append(run_id, "allowed", tool_call_id=call.id, risk=tool.risk)
        self.state.update(
            run_id,
            in_flight_tool_call={"tool_call_id": call.id, "tool": call.name, "arguments": call.arguments, "risk": tool.risk},
        )
        self._checkpoint(run_id, "tool_execution_started")
        self.audit.append(run_id, "executing", tool_call_id=call.id)
        try:
            output = tool.handler(**call.arguments)
        except Exception as exc:
            result = self._error(call, "TOOL_EXECUTION_FAILED", type(exc).__name__)
            self.audit.append(
                run_id,
                "result",
                tool_call_id=call.id,
                ok=False,
                code=result["code"],
                message=result["message"],
            )
            self.state.record_completed_call(run_id, call.id, result, action_key=action_key)
            self.state.update(run_id, in_flight_tool_call=None)
            self._checkpoint(run_id, "tool_result")
            return result
        result = {"ok": True, "tool_call_id": call.id, "content": output}
        self.audit.append(run_id, "result", tool_call_id=call.id, ok=True)
        self.state.record_completed_call(run_id, call.id, result, action_key=action_key)
        self.state.update(run_id, in_flight_tool_call=None)
        if tool.risk == "read":
            self.state.add_evidence(
                run_id,
                kind="NOTE",
                tool_call_id=call.id,
                data={"tool": call.name},
                workspace_digest=self.workspace_digest(),
            )
        elif tool.risk == "write":
            self.state.add_evidence(
                run_id,
                kind="CHANGE",
                tool_call_id=call.id,
                data={"tool": call.name, "result": output},
                workspace_digest=self.workspace_digest(),
            )
        elif tool.risk == "execute" and isinstance(output, dict) and output.get("exit_code") == 0:
            self.state.add_evidence(
                run_id,
                kind="TEST",
                tool_call_id=call.id,
                data={"tool": call.name, "result": output},
                workspace_digest=self.workspace_digest(),
            )
        self._checkpoint(run_id, "tool_result")
        return result
