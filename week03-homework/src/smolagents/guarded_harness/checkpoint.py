"""Safe-boundary snapshots for GuardedCodingHarness."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .state_store import StateStore


class CheckpointStore:
    def __init__(self, database: str | Path, state: StateStore):
        self.database = str(database)
        self.state = state
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS guarded_checkpoints (run_id TEXT PRIMARY KEY, checksum TEXT NOT NULL, payload_json TEXT NOT NULL, reason TEXT NOT NULL)"
            )

    @staticmethod
    def _canonical(state: dict[str, Any]) -> str:
        return json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def save(self, run_id: str, *, reason: str) -> None:
        state = self.state.get_run(run_id)
        payload = self._canonical(state)
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO guarded_checkpoints(run_id, checksum, payload_json, reason) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET checksum=excluded.checksum, payload_json=excluded.payload_json, reason=excluded.reason",
                (run_id, checksum, payload, reason),
            )

    def load(self, run_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT checksum, payload_json FROM guarded_checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No checkpoint for run: {run_id}")
        checksum, payload = row
        if hashlib.sha256(payload.encode()).hexdigest() != checksum:
            raise RuntimeError("CHECKPOINT_CHECKSUM_MISMATCH")
        return json.loads(payload)

    def restore(self, run_id: str) -> dict[str, Any]:
        state = self.load(run_id)
        self.state._save(state)
        return state
