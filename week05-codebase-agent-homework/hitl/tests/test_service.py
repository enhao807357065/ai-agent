from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service


def request(ticket_id: str = "ticket-1") -> dict:
    return {
        "ticket_id": ticket_id,
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "requester",
        "roles": ["developer"],
        "tool_name": "write_file",
        "args_digest": "digest-1",
        "workspace": "/repo",
        "risk": "write",
        "reason": "approval required",
        "approvers": ["maintainer"],
    }


def test_ticket_survives_service_restart_and_has_single_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HITL_API_KEY", "test-key")
    database = tmp_path / "tickets.db"
    service.set_database_path(database)
    headers = {"Authorization": "Bearer test-key"}

    with TestClient(service.app) as client:
        created = client.post("/tickets", json=request(), headers=headers)
        assert created.status_code == 200
        assert created.json()["status"] == "awaiting_human"

    service.set_database_path(database)
    with TestClient(service.app) as restarted:
        restored = restarted.get("/tickets/ticket-1", headers=headers)
        assert restored.status_code == 200
        assert restored.json()["status"] == "awaiting_human"

        approved = restarted.post(
            "/tickets/ticket-1/resume",
            json={"action": "approve", "actor": "reviewer", "roles": ["maintainer"]},
            headers=headers,
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        again = restarted.post(
            "/tickets/ticket-1/resume",
            json={"action": "approve", "actor": "reviewer", "roles": ["maintainer"]},
            headers=headers,
        )
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "TICKET_NOT_AWAITING_HUMAN"
