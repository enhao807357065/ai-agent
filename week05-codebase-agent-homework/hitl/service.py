"""Durable FastAPI approval tickets backed by local SQLite.

Run: HITL_API_KEY=dev-key uvicorn service:app --host 127.0.0.1 --port 8383
HITL_DB may point at a different SQLite file. The LangGraph graph evaluates a
single decision; SQLite is the durable source of truth for tickets across restarts.
"""
from __future__ import annotations
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from graph import ApprovalState, build_approval_graph

app = FastAPI(title="Codebase Agent HITL", version="2.0.0")
approval_graph = build_approval_graph()
DB_PATH = Path(os.environ.get("HITL_DB", Path(__file__).with_name("hitl.db")))

def set_database_path(path: str | Path) -> None:
    """Test hook: switch the SQLite ticket store before an API request."""
    global DB_PATH
    DB_PATH = Path(path)

class CreateTicket(BaseModel):
    ticket_id: str; trace_id: str; tenant_id: str; user_id: str
    roles: list[str] = Field(default_factory=list)
    tool_name: str; args_digest: str; workspace: str; risk: str; reason: str
    approvers: list[str] = Field(default_factory=list)

class DecideTicket(BaseModel):
    action: Literal["approve", "reject"]; actor: str
    roles: list[str] = Field(default_factory=list); feedback: str = ""

class ApprovalTicket(CreateTicket):
    requested_by: str
    status: Literal["awaiting_human", "approved", "rejected"]
    code: str = ""; feedback: str = ""; decided_by: str | None = None

@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS tickets (
          ticket_id TEXT PRIMARY KEY, immutable_json TEXT NOT NULL, requested_by TEXT NOT NULL,
          status TEXT NOT NULL, code TEXT NOT NULL, feedback TEXT NOT NULL, decided_by TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
        )""")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()

def now() -> str: return datetime.now(timezone.utc).isoformat()
def canonical(model: CreateTicket) -> str: return json.dumps(model.model_dump(), sort_keys=True, separators=(",", ":"))
def row_ticket(row: sqlite3.Row) -> ApprovalTicket:
    immutable = json.loads(row["immutable_json"])
    return ApprovalTicket(**immutable, requested_by=row["requested_by"], status=row["status"], code=row["code"], feedback=row["feedback"], decided_by=row["decided_by"])
def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("HITL_API_KEY")
    if not expected or authorization != f"Bearer {expected}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
def ticket_or_404(connection: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if not row: raise HTTPException(status_code=404, detail="ticket not found")
    return row

@app.post("/tickets", response_model=ApprovalTicket, dependencies=[Depends(require_api_key)])
def create_ticket(request: CreateTicket) -> ApprovalTicket:
    payload = canonical(request)
    with database() as connection:
        existing = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (request.ticket_id,)).fetchone()
        if existing:
            if existing["immutable_json"] != payload:
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_CONFLICT", "message": "ticket ID is bound to different request"})
            return row_ticket(existing)
        timestamp = now()
        connection.execute("INSERT INTO tickets (ticket_id, immutable_json, requested_by, status, code, feedback, created_at, updated_at) VALUES (?, ?, ?, 'awaiting_human', 'AWAITING_HUMAN', '', ?, ?)", (request.ticket_id, payload, request.user_id, timestamp, timestamp))
        return ApprovalTicket(**request.model_dump(), requested_by=request.user_id, status="awaiting_human", code="AWAITING_HUMAN")

@app.get("/tickets", response_model=dict[str, list[ApprovalTicket]], dependencies=[Depends(require_api_key)])
def list_tickets() -> dict[str, list[ApprovalTicket]]:
    with database() as connection:
        return {"tickets": [row_ticket(row) for row in connection.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()]}

@app.get("/tickets/{ticket_id}", response_model=ApprovalTicket, dependencies=[Depends(require_api_key)])
def get_ticket(ticket_id: str) -> ApprovalTicket:
    with database() as connection: return row_ticket(ticket_or_404(connection, ticket_id))

@app.post("/tickets/{ticket_id}/resume", response_model=ApprovalTicket, dependencies=[Depends(require_api_key)])
def decide_ticket(ticket_id: str, decision: DecideTicket) -> ApprovalTicket:
    with database() as connection:
        row = ticket_or_404(connection, ticket_id)
        if row["status"] != "awaiting_human":
            raise HTTPException(status_code=409, detail={"code": "TICKET_NOT_AWAITING_HUMAN", "message": "ticket is already decided"})
        ticket = row_ticket(row)
        graph_input: ApprovalState = {"requested_by": ticket.requested_by, "approvers": ticket.approvers, "action": decision.action, "actor": decision.actor, "roles": decision.roles, "feedback": decision.feedback, "status": "rejected", "code": ""}
        result = approval_graph.invoke(graph_input)
        changed = connection.execute("UPDATE tickets SET status = ?, code = ?, feedback = ?, decided_by = ?, updated_at = ?, version = version + 1 WHERE ticket_id = ? AND status = 'awaiting_human' AND version = ?", (result["status"], result["code"], result["feedback"], decision.actor, now(), ticket_id, row["version"])).rowcount
        if changed != 1: raise HTTPException(status_code=409, detail={"code": "TICKET_DECISION_RACE", "message": "ticket was decided concurrently"})
        return ApprovalTicket(**ticket.model_dump(exclude={"requested_by", "status", "code", "feedback", "decided_by"}), requested_by=ticket.requested_by, status=result["status"], code=result["code"], feedback=result["feedback"], decided_by=decision.actor)
