import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { canonicalJson, type PlatformStore } from "../store.js";
import type { ExecutionContext } from "../domain.js";
import type { Plan } from "../planning/plan-store.js";

export type RunStatus = "running" | "waiting_approval" | "paused" | "completed" | "failed" | "cancelled" | "needs_review";
export interface DurableRun {
  readonly version: 1;
  readonly id: string;
  readonly traceId: string;
  readonly context: ExecutionContext;
  readonly prompt: string;
  readonly task: string | null;
  readonly workspace: { readonly root: string; readonly initialDigest: string; readonly digest: string };
  readonly status: RunStatus;
  readonly messages: readonly unknown[];
  /** Exact number of scripted assistant responses already consumed; do not infer from Pi message count. */
  readonly scriptedResponseCursor?: number;
  /** Durable code revision; restored before recreating PlanStore so evidence stays comparable across resume. */
  readonly codeRevision?: number;
  /** Whether this run uses explicit model-visible planning even before create_plan activates a Plan. */
  readonly planningEnabled?: boolean;
  readonly plan?: Plan;
  readonly activePlanId?: string;
  readonly pendingApprovalId?: string;
  readonly result?: unknown;
}

export class RunCheckpointError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = "RunCheckpointError"; }
}

/** Deterministic manifest; any unreadable/missing file makes digest creation fail rather than silently skipping it. */
export function workspaceDigest(root: string): string {
  const entries: string[] = [];
  const excluded = new Set([".git", "node_modules", "platform.db", "platform.db-shm", "platform.db-wal", "artifacts"]);
  const walk = (directory: string): void => {
    for (const name of readdirSync(directory).sort((a, b) => a.localeCompare(b, "en"))) {
      if (excluded.has(name)) continue;
      const path = join(directory, name);
      const stat = statSync(path);
      if (stat.isDirectory()) walk(path);
      else if (stat.isFile()) entries.push(`${relative(root, path)}:${createHash("sha256").update(readFileSync(path)).digest("hex")}`);
      else throw new RunCheckpointError("WORKSPACE_DIGEST_UNSUPPORTED", `Unsupported workspace entry: ${path}`);
    }
  };
  walk(root);
  return createHash("sha256").update(entries.join("\n")).digest("hex");
}

export function runChecksum(run: DurableRun): string {
  return createHash("sha256").update(canonicalJson(run)).digest("hex");
}

export class RunCheckpointStore {
  constructor(private readonly store: PlatformStore) {}

  save(run: DurableRun): void {
    const digest = workspaceDigest(run.workspace.root);
    if (digest !== run.workspace.digest) throw new RunCheckpointError("WORKSPACE_DIGEST_MISMATCH", "Refusing to save a run whose workspace evidence is stale.");
    const payload = canonicalJson(run);
    this.store.saveRun({
      id: run.id,
      traceId: run.traceId,
      status: run.status,
      workspace: run.workspace.root,
      workspaceDigest: digest,
      payloadJson: payload,
      checksum: runChecksum(run),
      pendingApprovalId: run.pendingApprovalId,
    });
  }

  load(id: string): DurableRun {
    const row = this.store.getRun(id);
    if (!row) throw new RunCheckpointError("RUN_NOT_FOUND", `Run ${id} does not exist.`);
    const payload = String(row.payload_json);
    let run: DurableRun;
    try { run = JSON.parse(payload) as DurableRun; } catch { throw new RunCheckpointError("RUN_CHECKPOINT_INVALID", "Run payload is not valid JSON."); }
    if (runChecksum(run) !== String(row.checksum)) throw new RunCheckpointError("RUN_CHECKSUM_MISMATCH", "Run checkpoint integrity check failed.");
    if (run.id !== id || run.traceId !== String(row.trace_id)) throw new RunCheckpointError("RUN_IDENTITY_MISMATCH", "Run identity does not match its checkpoint row.");
    return run;
  }

  prepareResume(run: DurableRun): DurableRun {
    if (run.status !== "waiting_approval") {
      this.assertResumable(run);
      return run;
    }
    if (!run.pendingApprovalId) {
      throw new RunCheckpointError("RUN_APPROVAL_MISSING", `Run ${run.id} is waiting without a pending approval.`);
    }
    const approval = this.store.getApproval(run.pendingApprovalId);
    if (!approval) {
      throw new RunCheckpointError("RUN_APPROVAL_MISSING", `Approval ${run.pendingApprovalId} does not exist.`);
    }
    if (approval.trace_id !== run.traceId) {
      throw new RunCheckpointError("RUN_APPROVAL_BINDING_MISMATCH", "Pending approval does not belong to this run.");
    }
    if (["pending", "approved"].includes(approval.status)) {
      throw new RunCheckpointError("RUN_PENDING_APPROVAL", "Approve and execute the frozen action before resuming.");
    }
    if (approval.status === "executing") {
      throw new RunCheckpointError("RUN_APPROVAL_EXECUTING", "The frozen action is executing; recovery requires operator review.");
    }
    if (approval.status === "consumed") {
      const { pendingApprovalId: _pendingApprovalId, ...rest } = run;
      const resumed: DurableRun = {
        ...rest,
        status: "paused",
        workspace: { ...run.workspace, digest: workspaceDigest(run.workspace.root) },
      };
      this.save(resumed);
      return resumed;
    }
    const { pendingApprovalId: _pendingApprovalId, ...rest } = run;
    const failed: DurableRun = { ...rest, status: "failed" };
    this.save(failed);
    throw new RunCheckpointError(`RUN_APPROVAL_${approval.status.toUpperCase()}`, `The frozen action ended as ${approval.status}; this run will not replay it.`);
  }

  assertResumable(run: DurableRun): void {
    if (["completed", "failed", "cancelled"].includes(run.status)) throw new RunCheckpointError("RUN_TERMINAL", `Run ${run.id} is terminal.`);
    if (run.status === "waiting_approval") throw new RunCheckpointError("RUN_PENDING_APPROVAL", "Approve and execute the frozen action before resuming.");
    const actual = workspaceDigest(run.workspace.root);
    if (actual !== run.workspace.digest) throw new RunCheckpointError("WORKSPACE_DRIFT", "Workspace changed since its checkpoint; resume is refused.");
  }
}
