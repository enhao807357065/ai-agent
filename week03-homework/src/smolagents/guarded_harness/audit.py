"""Append-only SQLite audit ledger for guarded tool calls."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class AuditLog:
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
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tool_call_id TEXT,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def append(self, run_id: str, event: str, *, tool_call_id: str | None = None, **payload: Any) -> dict[str, Any]:
        decision = payload.pop("decision", None)
        if decision is None:
            decision = "rejected" if event == "rejected" else "recorded"
        record = {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "toolCallId": tool_call_id,
            "event": event,
            "decision": decision,
            "actor": payload.pop("actor", "system"),
            **payload,
        }
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO audit_events(run_id, tool_call_id, event, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, tool_call_id, event, json.dumps(record, sort_keys=True, ensure_ascii=False)),
            )
        return {"sequence": cursor.lastrowid, **record}

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, payload_json FROM audit_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [{"sequence": row["sequence"], **json.loads(row["payload_json"])} for row in rows]

    def for_tool_call(self, tool_call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, payload_json FROM audit_events WHERE tool_call_id = ? ORDER BY sequence", (tool_call_id,)
            ).fetchall()
        return [{"sequence": row["sequence"], **json.loads(row["payload_json"])} for row in rows]
