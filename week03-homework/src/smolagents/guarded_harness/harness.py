"""Assembly layer: smolagents ToolCallingAgent + durable guarded repository runtime."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from smolagents import ToolCallingAgent
from smolagents.utils import AgentError, AgentGenerationError

from .audit import AuditLog
from .checkpoint import CheckpointStore
from .gate import Gate, ToolCall
from .sandbox import Sandbox
from .state_store import StateStore


@dataclass(frozen=True)
class TaskPolicy:
    kind: str
    approval_required: bool
    command_allowlist: frozenset[str]
    allowed_risks: frozenset[str]

    @classmethod
    def code_understanding(cls, *, approval_required: bool = True) -> "TaskPolicy":
        # Understanding may emit a durable report. Its write capability is therefore
        # available, but must be human-approved by default.
        return cls("code_understanding", approval_required, frozenset({"python", "pytest", "git"}), frozenset({"read", "write"}))

    @classmethod
    def testing_fix(cls, *, approval_required: bool = True) -> "TaskPolicy":
        return cls(
            "testing_fix",
            approval_required,
            frozenset({"python", "pytest", "git", "node", "npm", "npx"}),
            frozenset({"read", "write", "execute"}),
        )


class GuardedToolCallingAgent(ToolCallingAgent):
    """Adapter that sends every smolagents tool call through one Gate.

    It deliberately changes only the public tool execution seam. Model planning,
    parsing and memory continue to be owned by ToolCallingAgent.
    """

    def __init__(self, *args: Any, gate: Gate, run_id: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.gate = gate
        self.guarded_run_id = run_id
        self._current_tool_call_id: str | None = None

    def process_tool_calls(self, chat_message, memory_step):  # type: ignore[no-untyped-def]
        # ToolCallingAgent owns parsing/memory. Bind each emitted provider ID while
        # its standard method invokes execute_tool_call, then clear it afterward.
        original = self.execute_tool_call
        calls = iter(chat_message.tool_calls or [])

        def guarded_execute(tool_name: str, arguments: dict[str, Any] | str):
            call = next(calls)
            if tool_name == "final_answer":
                answer = arguments.get("answer", "") if isinstance(arguments, dict) else str(arguments)
                result = self.gate.final_answer(self.guarded_run_id, call.id, str(answer))
                return result.get("content", result)
            raw_arguments = arguments if isinstance(arguments, dict) else {"value": arguments}
            result = self.gate.invoke(self.guarded_run_id, ToolCall(call.id, tool_name, raw_arguments))
            if not result["ok"]:
                return result
            return result["content"]

        self.execute_tool_call = guarded_execute  # type: ignore[method-assign]
        try:
            yield from super().process_tool_calls(chat_message, memory_step)
        finally:
            self.execute_tool_call = original  # type: ignore[method-assign]
    def _step_stream(self, memory_step):  # type: ignore[no-untyped-def]
        if self.gate.state.get_run(self.guarded_run_id).get("status") == "stopped":
            raise AgentGenerationError("guarded run stopped", self.logger)
        yield from super()._step_stream(memory_step)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Do not let framework retries continue after a governance stop."""
        try:
            return super().run(*args, **kwargs)
        except AgentError:
            state = self.gate.state.get_run(self.guarded_run_id)
            if state.get("status") == "stopped":
                return {"ok": False, "code": state["stop_reason"], "message": "guarded run stopped"}
            raise

    def _handle_max_steps_reached(self, task: str) -> Any:
        self.gate.state.set_stop(self.guarded_run_id, status="stopped", reason="MAX_STEPS_REACHED")
        self.gate.audit.append(
            self.guarded_run_id,
            "stopped",
            rule="MAX_STEPS_REACHED",
            decision="stopped",
            actor="system",
        )
        return {"ok": False, "code": "MAX_STEPS_REACHED", "message": "agent step budget exhausted"}


class GuardedCodingHarness:
    def __init__(self, *, workspace: Path, database: Path, run_id: str, policy: TaskPolicy, state: StateStore, audit: AuditLog):
        self.workspace = workspace.resolve()
        self.database = database
        self.run_id = run_id
        self.policy = policy
        self.state = state
        self.audit = audit
        self.sandbox = Sandbox(workspace=self.workspace, command_allowlist=set(policy.command_allowlist))
        self.checkpoint = CheckpointStore(database, state)
        self.gate = Gate(
            state=state,
            audit=audit,
            approval_required=policy.approval_required,
            allowed_risks=set(policy.allowed_risks),
            path_validator=self.sandbox._path,
            checkpoint=self._checkpoint,
            workspace_digest=self._current_workspace_digest,
            completion=self.finalize,
        )

    @staticmethod
    def _workspace_digest(workspace: Path) -> str:
        digest = hashlib.sha256()
        protected = {".git", ".env", ".ssh", "secrets"}
        internal_prefixes = (".guarded-harness.sqlite",)
        for item in sorted(workspace.rglob("*"), key=lambda path: str(path)):
            if not item.is_file() or item.is_symlink():
                continue
            relative = item.relative_to(workspace)
            if any(part in protected for part in relative.parts) or relative.name.startswith(internal_prefixes):
                continue
            stat = item.stat()
            digest.update(str(relative).encode())
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode())
            digest.update(b"\0")
            with item.open("rb") as source:
                for chunk in iter(lambda: source.read(64 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def _checkpoint(self, reason: str) -> None:
        self.state.update(self.run_id, workspace_digest=self._workspace_digest(self.workspace))
        self.checkpoint.save(self.run_id, reason=reason)

    @classmethod
    def create(cls, *, workspace: str | Path, database: str | Path, task: TaskPolicy, prompt: str = "") -> "GuardedCodingHarness":
        workspace_path, database_path = Path(workspace), Path(database)
        if workspace_path.exists() and not workspace_path.is_dir():
            raise ValueError(f"WORKSPACE_NOT_DIRECTORY: {workspace_path}")
        workspace_path.mkdir(parents=True, exist_ok=True)
        if database_path.parent != Path(".") and not database_path.parent.is_dir():
            raise ValueError(f"DATABASE_PARENT_NOT_DIRECTORY: {database_path.parent}")
        state, audit = StateStore(database_path), AuditLog(database_path)
        run_id = uuid4().hex
        state.create_run(run_id, workspace=str(workspace_path.resolve()), task_kind=task.kind, prompt=prompt)
        audit.append(run_id, "run_created", task_kind=task.kind)
        return cls(workspace=workspace_path, database=database_path, run_id=run_id, policy=task, state=state, audit=audit)

    @classmethod
    def from_checkpoint(cls, database: str | Path, run_id: str, *, workspace: str | Path) -> "GuardedCodingHarness":
        database_path, workspace_path = Path(database), Path(workspace)
        state, audit = StateStore(database_path), AuditLog(database_path)
        saved = state.get_run(run_id)
        policy = TaskPolicy.testing_fix(approval_required=bool(saved["approvals"])) if saved["task_kind"] == "testing_fix" else TaskPolicy.code_understanding()
        harness = cls(workspace=workspace_path, database=database_path, run_id=run_id, policy=policy, state=state, audit=audit)
        checkpoint_state: dict[str, Any] | None = None
        try:
            checkpoint_state = harness.checkpoint.load(run_id)
        except KeyError:
            harness._checkpoint("recovery_from_live_state")
            audit.append(run_id, "checkpoint_created_from_live_state")
        if checkpoint_state is not None:
            expected_digest = checkpoint_state.get("workspace_digest")
            actual_digest = harness._workspace_digest(harness.workspace)
            in_flight = checkpoint_state.get("in_flight_tool_call")
            if in_flight is not None:
                state.update(
                    run_id,
                    status="waiting_verification",
                    stop_reason="UNKNOWN_RESULT",
                    in_flight_tool_call={**in_flight, "status": "UNKNOWN_RESULT"},
                )
                audit.append(run_id, "unknown_result", tool_call_id=in_flight["tool_call_id"], rule="UNKNOWN_RESULT", decision="wait_for_verification")
            elif expected_digest is not None and expected_digest != actual_digest:
                state.update(run_id, status="waiting_verification", stop_reason="WORKSPACE_CHANGED")
                audit.append(run_id, "workspace_changed", expected_digest=expected_digest, actual_digest=actual_digest)
            else:
                harness.checkpoint.restore(run_id)
        audit.append(run_id, "resumed")
        return harness

    def register_tool(self, name: str, schema: dict[str, type], handler, *, risk: str) -> None:  # type: ignore[no-untyped-def]
        self.gate.register(name, schema, handler, risk=risk)

    def register_repository_tools(self) -> None:
        self.register_tool("read_file", {"path": str}, self.sandbox.read_text, risk="read")
        self.register_tool("write_file", {"path": str, "content": str}, self._write_file, risk="write")
        self.register_tool("run_test", {"argv": list}, self.sandbox.run_command, risk="execute")

    def _write_file(self, path: str, content: str) -> dict[str, str]:
        self.sandbox.write_text(path, content)
        return {"path": path}

    def approve(self, approval_id: str, *, actor: str) -> bool:
        approved = self.state.approve(self.run_id, approval_id, actor)
        self.audit.append(self.run_id, "approval_decided", approval_id=approval_id, approved=approved, actor=actor)
        if approved:
            self.checkpoint.save(self.run_id, reason="approval_decided")
        return approved

    def resume_approval(self, approval_id: str) -> dict[str, Any] | None:
        state = self.state.get_run(self.run_id)
        if state.get("status") == "waiting_verification":
            return None
        if state.get("pending_approval") != approval_id:
            return None
        approval = next((item for item in state["approvals"] if item["approval_id"] == approval_id), None)
        if approval is None or approval["status"] != "approved":
            return None
        self.state.update(self.run_id, pending_approval=None, status="running")
        result = self.gate.invoke(
            self.run_id,
            ToolCall(approval["tool_call_id"], approval["tool"], approval["arguments"]),
            resume_approved=True,
        )
        self.audit.append(self.run_id, "approval_consumed", approval_id=approval_id, tool_call_id=approval["tool_call_id"])
        self.checkpoint.save(self.run_id, reason="approved_tool_boundary")
        return result

    def resume(self) -> dict[str, Any] | None:
        """Compatibility API; CLI must call resume_approval with an approval ID."""
        state = self.state.get_run(self.run_id)
        approval_id = state.get("pending_approval")
        return self.resume_approval(approval_id) if approval_id else None

    def build_agent(self, *, model: Any, tools: list[Any], **kwargs: Any) -> GuardedToolCallingAgent:
        """The one model entrypoint; expose business tools only after Gate registration."""
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
        for tool in tools:
            if tool.name in self.gate.tools:
                continue
            schema = {
                name: type_map.get(str(spec["type"]), object)
                for name, spec in tool.inputs.items()
            }

            def invoke_tool(_tool=tool, **arguments: Any) -> Any:
                return _tool(**arguments, sanitize_inputs_outputs=True)

            self.register_tool(tool.name, schema, invoke_tool, risk="read")
        return GuardedToolCallingAgent(tools=tools, model=model, gate=self.gate, run_id=self.run_id, **kwargs)

    def trace(self) -> dict[str, Any]:
        """Server-owned evidence view for one complete guarded task."""
        state = self.state.get_run(self.run_id)
        events = self.audit.for_run(self.run_id)
        sandbox_events = [
            event
            for event in events
            if event["event"] in {"rejected", "allowed", "executing", "result", "workspace_changed", "unknown_result"}
        ]
        return {
            "run_id": self.run_id,
            "state": state,
            "gate": events,
            "sandbox": sandbox_events,
            "approvals": state["approvals"],
            "results": state["completed_tool_calls"],
            "evidence": state["evidence"],
        }

    @staticmethod
    def _requested_deliverables(prompt: str) -> list[str]:
        """Extract explicit workspace-relative report paths from the task contract."""
        import re

        return re.findall(r"(?<!\S)((?:artifacts|docs|reports)/[A-Za-z0-9_./-]+)", prompt)

    def _current_workspace_digest(self) -> str:
        return self._workspace_digest(self.workspace)

    def finalize(self, answer: str) -> dict[str, Any]:
        state = self.state.get_run(self.run_id)
        if state.get("status") == "stopped":
            return {"status": "failed", "stop_reason": state["stop_reason"], "answer": answer, "issues": ["run stopped before final acceptance"]}
        issues: list[str] = []
        requested_deliverables = self._requested_deliverables(state["prompt"])
        for path in requested_deliverables:
            if not (self.workspace / path).is_file():
                issues.append(f"requested deliverable is missing: {path}")
        if self.policy.kind == "code_understanding" and requested_deliverables:
            written_paths: set[str] = set()
            for evidence in state["evidence"]:
                if evidence["kind"] != "CHANGE":
                    continue
                output = evidence.get("data", {}).get("result")
                if isinstance(output, dict) and isinstance(output.get("path"), str):
                    written_paths.add(output["path"])
                elif isinstance(output, str) and output.startswith("wrote "):
                    written_paths.add(output.removeprefix("wrote "))
            for path in requested_deliverables:
                if path not in written_paths:
                    issues.append(f"deliverable lacks server-owned CHANGE evidence: {path}")
        if self.policy.kind == "testing_fix":
            kinds = {evidence["kind"] for evidence in state["evidence"]}
            if "CHANGE" not in kinds:
                issues.append("CHANGE evidence is required for a testing_fix task")
            current_digest = self._current_workspace_digest()
            current_tests = [
                evidence
                for evidence in state["evidence"]
                if evidence["kind"] == "TEST" and evidence.get("workspace_digest") == current_digest
            ]
            if not current_tests:
                issues.append("successful TEST evidence is required for the current workspace version")
        status = "completed" if not issues else "failed"
        reason = "FINAL_ACCEPTANCE_PASSED" if not issues else "FINAL_ACCEPTANCE_FAILED"
        result = {"status": status, "stop_reason": reason, "answer": answer, "issues": issues}
        self.state.set_stop(self.run_id, status=status, reason=reason, result=result)
        self.audit.append(self.run_id, "finalized", status=status, stop_reason=reason, issues=issues)
        self.checkpoint.save(self.run_id, reason="final_acceptance")
        return result
