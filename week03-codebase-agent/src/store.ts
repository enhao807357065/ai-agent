import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { ExecutionContext } from "./domain.js";

export interface ApprovalRecord {
  id: string;
  trace_id: string;
  tool_call_id: string;
  tool_name: string;
  context_json: string;
  args_json: string;
  args_digest: string;
  status: string;
  expires_at: string;
  decided_by: string | null;
  created_at: string;
}

export function canonicalJson(value: unknown): string {
  // JSON.stringify(undefined) returns undefined rather than valid JSON text. Durable
  // checkpoints include optional fields, so omit object keys and render array slots as
  // null exactly like JSON.stringify; never serialize the literal token `undefined`.
  if (value === undefined) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function approvalDigest(input: {
  traceId: string;
  toolCallId: string;
  toolName: string;
  context: ExecutionContext;
  args: Record<string, unknown>;
}): string {
  return createHash("sha256")
    .update(
      canonicalJson({
        trace_id: input.traceId,
        tool_call_id: input.toolCallId,
        tool_name: input.toolName,
        user_id: input.context.userId,
        tenant_id: input.context.tenantId,
        workspace: input.context.workspace ?? "",
        args: input.args,
      }),
    )
    .digest("hex");
}

export class PlatformStore {
  private readonly db: DatabaseSync;
  private readonly artifactDir: string;

  constructor(path: string) {
    mkdirSync(dirname(path), { recursive: true });
    this.artifactDir = join(dirname(path), "artifacts");
    mkdirSync(this.artifactDir, { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;");
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS audit_records (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        status TEXT NOT NULL,
        code TEXT NOT NULL,
        input_json TEXT NOT NULL,
        output_json TEXT,
        latency_ms INTEGER NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_records(trace_id, created_at);

      CREATE TABLE IF NOT EXISTS trace_events (
        id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        span_id TEXT NOT NULL,
        parent_span_id TEXT,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_trace_events ON trace_events(trace_id, created_at);

      CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        context_json TEXT NOT NULL,
        args_json TEXT NOT NULL,
        args_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_by TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        consumed_at TEXT,
        error_code TEXT
      );

      CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        tenant_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        plan_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        workspace TEXT NOT NULL,
        workspace_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        pending_approval_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, updated_at DESC);
    `);
  }

  addAudit(record: {
    eventType: string;
    context: ExecutionContext;
    toolCallId: string;
    toolName: string;
    status: string;
    code: string;
    input: unknown;
    output?: unknown;
    latencyMs?: number;
  }): void {
    this.db
      .prepare(`
        INSERT INTO audit_records (
          id, event_type, trace_id, tool_call_id, tool_name, user_id, tenant_id,
          status, code, input_json, output_json, latency_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `)
      .run(
        randomUUID(),
        record.eventType,
        record.context.traceId,
        record.toolCallId,
        record.toolName,
        record.context.userId,
        record.context.tenantId,
        record.status,
        record.code,
        JSON.stringify(record.input),
        record.output === undefined ? null : JSON.stringify(record.output),
        record.latencyMs ?? 0,
        new Date().toISOString(),
      );
  }

  listAudit(traceId?: string): Record<string, unknown>[] {
    const statement = traceId
      ? this.db.prepare("SELECT * FROM audit_records WHERE trace_id = ? ORDER BY created_at, rowid")
      : this.db.prepare("SELECT * FROM audit_records ORDER BY created_at DESC, rowid DESC LIMIT 100");
    return (traceId ? statement.all(traceId) : statement.all()) as Record<string, unknown>[];
  }

  saveResultArtifact(input: { traceId: string; toolCallId: string; content: string }): string {
    const safeTrace = input.traceId.replace(/[^a-zA-Z0-9_-]/g, "_");
    const safeCall = input.toolCallId.replace(/[^a-zA-Z0-9_-]/g, "_");
    const path = join(this.artifactDir, `${safeTrace}-${safeCall}.log`);
    writeFileSync(path, input.content, "utf8");
    return path;
  }

  addTrace(input: {
    traceId: string;
    spanId?: string;
    parentSpanId?: string;
    eventType: string;
    payload?: unknown;
  }): void {
    this.db
      .prepare(`
        INSERT INTO trace_events (id, trace_id, span_id, parent_span_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
      `)
      .run(
        randomUUID(),
        input.traceId,
        input.spanId ?? randomUUID(),
        input.parentSpanId ?? null,
        input.eventType,
        JSON.stringify(input.payload ?? {}),
        new Date().toISOString(),
      );
  }

  getTrace(traceId: string): Record<string, unknown>[] {
    return this.db
      .prepare("SELECT * FROM trace_events WHERE trace_id = ? ORDER BY created_at, rowid")
      .all(traceId) as Record<string, unknown>[];
  }

  createApproval(input: {
    context: ExecutionContext;
    toolCallId: string;
    toolName: string;
    args: Record<string, unknown>;
    ttlSeconds?: number;
  }): ApprovalRecord {
    const id = randomUUID();
    const now = new Date();
    const expiresAt = new Date(now.getTime() + (input.ttlSeconds ?? 900) * 1_000);
    const digest = approvalDigest({
      traceId: input.context.traceId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      context: input.context,
      args: input.args,
    });
    this.db
      .prepare(`
        INSERT INTO approvals (
          id, trace_id, tool_call_id, tool_name, context_json, args_json,
          args_digest, status, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
      `)
      .run(
        id,
        input.context.traceId,
        input.toolCallId,
        input.toolName,
        JSON.stringify(input.context),
        JSON.stringify(input.args),
        digest,
        expiresAt.toISOString(),
        now.toISOString(),
      );
    return this.getApproval(id)!;
  }

  getApproval(id: string): ApprovalRecord | undefined {
    return this.db.prepare("SELECT * FROM approvals WHERE id = ?").get(id) as ApprovalRecord | undefined;
  }

  savePlan(id: string, planJson: string, checksum: string): void {
    this.db
      .prepare(`
        INSERT INTO plans (id, plan_json, checksum, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET plan_json = excluded.plan_json, checksum = excluded.checksum, updated_at = excluded.updated_at
      `)
      .run(id, planJson, checksum, new Date().toISOString());
  }

  getPlan(id: string): { id: string; plan_json: string; checksum: string; updated_at: string } | undefined {
    return this.db.prepare("SELECT id, plan_json, checksum, updated_at FROM plans WHERE id = ?").get(id) as
      | { id: string; plan_json: string; checksum: string; updated_at: string }
      | undefined;
  }

  saveRun(input: {
    id: string;
    traceId: string;
    status: string;
    workspace: string;
    workspaceDigest: string;
    payloadJson: string;
    checksum: string;
    pendingApprovalId?: string;
  }): void {
    const now = new Date().toISOString();
    this.db
      .prepare(`
        INSERT INTO runs (id, trace_id, status, workspace, workspace_digest, payload_json, checksum, pending_approval_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET status = excluded.status, workspace = excluded.workspace,
          workspace_digest = excluded.workspace_digest, payload_json = excluded.payload_json, checksum = excluded.checksum,
          pending_approval_id = excluded.pending_approval_id, updated_at = excluded.updated_at
      `)
      .run(input.id, input.traceId, input.status, input.workspace, input.workspaceDigest, input.payloadJson, input.checksum, input.pendingApprovalId ?? null, now, now);
  }

  getRun(id: string): Record<string, unknown> | undefined {
    return this.db.prepare("SELECT * FROM runs WHERE id = ?").get(id) as Record<string, unknown> | undefined;
  }

  listRuns(): Record<string, unknown>[] {
    return this.db.prepare("SELECT id, trace_id, status, workspace, pending_approval_id, created_at, updated_at FROM runs ORDER BY updated_at DESC").all() as Record<string, unknown>[];
  }

  listApprovals(status?: string): ApprovalRecord[] {
    const statement = status
      ? this.db.prepare("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC")
      : this.db.prepare("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 100");
    return (status ? statement.all(status) : statement.all()) as unknown as ApprovalRecord[];
  }

  decideApproval(id: string, decision: "approve" | "reject", actor: string): ApprovalRecord {
    const status = decision === "approve" ? "approved" : "rejected";
    const result = this.db
      .prepare(`
        UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?
        WHERE id = ? AND status = 'pending' AND expires_at > ?
      `)
      .run(status, actor, new Date().toISOString(), id, new Date().toISOString());
    if (Number(result.changes) !== 1) throw new Error("Approval is missing, expired, or already decided");
    return this.getApproval(id)!;
  }

  /**
   * 把审批图给出的终局答复抄回本地这张单，不抛异常：
   * 图那侧才是"人怎么决定"的权威，这里只保证本地状态不落后于它；真写不进去，执行阶段的 claim 会拦。
   */
  decideQuietly(id: string, decision: "approve" | "reject", actor: string): void {
    try {
      this.decideApproval(id, decision, actor);
    } catch {
      // 已经决定过或已过期：本地状态保持原样，由后面的 claim 判定能不能执行。
    }
  }

  /** 审批通道本身出问题（图连不上、没人有资格批）时关掉这张单，不让它永远挂在 pending。 */
  closeApproval(id: string, errorCode: string): void {
    this.db
      .prepare("UPDATE approvals SET status = 'denied', error_code = ?, decided_at = ? WHERE id = ? AND status = 'pending'")
      .run(errorCode, new Date().toISOString(), id);
  }

  claimApproval(id: string): boolean {
    const result = this.db
      .prepare("UPDATE approvals SET status = 'executing' WHERE id = ? AND status = 'approved' AND expires_at > ?")
      .run(id, new Date().toISOString());
    return Number(result.changes) === 1;
  }

  finishApproval(id: string, status: "consumed" | "failed", errorCode?: string): void {
    this.db
      .prepare("UPDATE approvals SET status = ?, consumed_at = ?, error_code = ? WHERE id = ? AND status = 'executing'")
      .run(status, new Date().toISOString(), errorCode ?? null, id);
  }

  createTicket(input: {
    idempotencyKey: string;
    tenantId: string;
    orderId: string;
    title: string;
  }): Record<string, unknown> {
    const existing = this.db
      .prepare("SELECT * FROM tickets WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as Record<string, unknown> | undefined;
    if (existing) return existing;
    const id = `T-${randomUUID().slice(0, 8)}`;
    this.db
      .prepare("INSERT INTO tickets (id, idempotency_key, tenant_id, order_id, title, created_at) VALUES (?, ?, ?, ?, ?, ?)")
      .run(id, input.idempotencyKey, input.tenantId, input.orderId, input.title, new Date().toISOString());
    return this.db.prepare("SELECT * FROM tickets WHERE id = ?").get(id) as Record<string, unknown>;
  }
}
