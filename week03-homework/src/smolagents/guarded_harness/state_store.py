"""Durable business state for guarded coding runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS guarded_runs (run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
            )

    def create_run(self, run_id: str, *, workspace: str, task_kind: str, prompt: str = "") -> dict[str, Any]:
        state = {
            "run_id": run_id,
            "workspace": workspace,
            "task_kind": task_kind,
            "prompt": prompt,
            "status": "running",
            "plan": [],
            "steps": [],
            "evidence": [],
            "budget": {"tool_calls": 0, "max_tool_calls": 50},
            "approvals": [],
            "completed_tool_calls": {},
            "completed_call_sequence": [],
            "in_flight_tool_call": None,
            "pending_approval": None,
            "stop_reason": None,
            "result": None,
        }
        self._save(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guarded_runs(run_id, state_json) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET state_json=excluded.state_json",
                (state["run_id"], json.dumps(state, sort_keys=True, ensure_ascii=False)),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT state_json FROM guarded_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return json.loads(row["state_json"])

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        state = self.get_run(run_id)
        state.update(changes)
        self._save(state)
        return state

    def add_step(self, run_id: str, name: str, status: str, *, success_criteria: str = "") -> dict[str, Any]:
        state = self.get_run(run_id)
        state["steps"].append({"name": name, "status": status, **({"success_criteria": success_criteria} if success_criteria else {})})
        self._save(state)
        return state

    def add_evidence(
        self,
        run_id: str,
        *,
        kind: str,
        tool_call_id: str,
        data: dict[str, Any],
        workspace_digest: str | None = None,
    ) -> dict[str, Any]:
        state = self.get_run(run_id)
        evidence = {"kind": kind, "tool_call_id": tool_call_id, "data": data}
        if workspace_digest is not None:
            evidence["workspace_digest"] = workspace_digest
        state["evidence"].append(evidence)
        self._save(state)
        return state

    def record_completed_call(
        self, run_id: str, tool_call_id: str, result: dict[str, Any], *, action_key: str | None = None
    ) -> dict[str, Any]:
        state = self.get_run(run_id)
        persisted = dict(result)
        if action_key is not None:
            persisted["_action_key"] = action_key
        state["completed_tool_calls"][tool_call_id] = persisted
        state.setdefault("completed_call_sequence", []).append(tool_call_id)
        state["budget"]["tool_calls"] += 1
        self._save(state)
        return state

    def add_approval(self, run_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        state = self.get_run(run_id)
        state["approvals"].append(approval)
        state["pending_approval"] = approval["approval_id"]
        state["status"] = "waiting_approval"
        self._save(state)
        return state

    def find_approval(self, approval_id: str) -> tuple[str, dict[str, Any]] | None:
        with self._connect() as connection:
            rows = connection.execute("SELECT run_id, state_json FROM guarded_runs").fetchall()
        matches: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            state = json.loads(row["state_json"])
            for approval in state["approvals"]:
                if approval["approval_id"] == approval_id:
                    matches.append((row["run_id"], approval))
        if len(matches) > 1:
            raise RuntimeError("APPROVAL_ID_NOT_UNIQUE")
        return matches[0] if matches else None

    def approve(self, run_id: str, approval_id: str, actor: str) -> bool:
        state = self.get_run(run_id)
        if state.get("status") == "waiting_verification":
            return False
        if state.get("pending_approval") != approval_id:
            return False
        for approval in state["approvals"]:
            if approval["approval_id"] == approval_id and approval["status"] == "pending":
                approval.update({"status": "approved", "actor": actor})
                # Approval decision does not execute the action. Keep the run
                # waiting until the explicit approval-bound resume command.
                state["status"] = "waiting_approval"
                self._save(state)
                return True
        return False

    def set_stop(self, run_id: str, *, status: str, reason: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.update(run_id, status=status, stop_reason=reason, result=result)
