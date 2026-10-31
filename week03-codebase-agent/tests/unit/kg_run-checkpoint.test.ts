import { describe, expect, it } from "vitest";
import { mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { PlatformStore } from "../../src/store.js";
import { ToolRuntime } from "../../src/runtime.js";
import { RunCheckpointError, RunCheckpointStore, workspaceDigest, type DurableRun } from "../../src/checkpoint/run-checkpoint.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";

function run(root: string): DurableRun {
  const digest = workspaceDigest(root);
  return {
    version: 1, id: randomUUID(), traceId: randomUUID(), prompt: "inspect", task: null, status: "paused",
    context: { traceId: randomUUID(), requestId: randomUUID(), userId: "user", tenantId: "tenant", roles: ["maintainer"], agentName: "agent", mode: "execute", workspace: root },
    workspace: { root, initialDigest: digest, digest }, messages: [],
  };
}

describe("durable run checkpoints", () => {
  it("lists run projections and returns the checksum-validated durable run for show", () => {
    const root = mkdtempSync(join(tmpdir(), "run-checkpoint-"));
    writeFileSync(join(root, "a.ts"), "export const a = 1;\n");
    const store = new PlatformStore(join(root, "platform.db"));
    const checkpoints = new RunCheckpointStore(store);
    const checkpoint = run(root);
    checkpoints.save(checkpoint);

    expect(store.listRuns()).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: checkpoint.id, trace_id: checkpoint.traceId, status: "paused", pending_approval_id: null }),
    ]));
    expect(checkpoints.load(checkpoint.id)).toMatchObject({ id: checkpoint.id, traceId: checkpoint.traceId, status: "paused" });
  });
  it("rejects a tampered SQLite payload before resume", () => {
    const root = mkdtempSync(join(tmpdir(), "run-checkpoint-"));
    writeFileSync(join(root, "a.ts"), "export const a = 1;\n");
    const store = new PlatformStore(join(root, "platform.db"));
    const checkpoints = new RunCheckpointStore(store);
    const checkpoint = run(root);
    checkpoints.save(checkpoint);
    store.saveRun({ id: checkpoint.id, traceId: checkpoint.traceId, status: "paused", workspace: root, workspaceDigest: checkpoint.workspace.digest, payloadJson: "{}", checksum: "bad" });
    expect(() => checkpoints.load(checkpoint.id)).toThrowError(expect.objectContaining({ code: "RUN_CHECKSUM_MISMATCH" }));
  });

  it("fails closed when workspace changed after checkpoint", () => {
    const root = mkdtempSync(join(tmpdir(), "run-checkpoint-"));
    writeFileSync(join(root, "a.ts"), "export const a = 1;\n");
    const checkpoints = new RunCheckpointStore(new PlatformStore(join(root, "platform.db")));
    const checkpoint = run(root);
    checkpoints.save(checkpoint);
    writeFileSync(join(root, "a.ts"), "export const a = 2;\n");
    expect(() => checkpoints.assertResumable(checkpoints.load(checkpoint.id))).toThrowError(expect.objectContaining({ code: "WORKSPACE_DRIFT" }));
  });

  it("keeps a pending durable approval waiting and executes its exact approved action once after restart", async () => {
    const harness = createWorkspaceHarness();
    const context = harness.context({ workspace: harness.workspace });
    const pending = await harness.runtime.invoke({
      context,
      toolCallId: "frozen-write",
      modelName: "write_file",
      args: { path: "docs/resume.md", content: "first\n" },
    });
    const checkpoints = new RunCheckpointStore(harness.store);
    const checkpoint: DurableRun = {
      ...run(harness.workspace),
      traceId: context.traceId,
      context,
      status: "waiting_approval",
      pendingApprovalId: pending.approvalId,
    };
    checkpoints.save(checkpoint);

    expect(() => checkpoints.prepareResume(checkpoints.load(checkpoint.id))).toThrowError(expect.objectContaining({ code: "RUN_PENDING_APPROVAL" }));

    harness.store.decideApproval(pending.approvalId!, "approve", "reviewer");
    const restarted = new ToolRuntime(harness.registry, harness.policy, harness.store);
    const executed = await restarted.executeApproval(pending.approvalId!);
    expect(executed).toMatchObject({ ok: true, code: "OK" });
    expect(harness.read("docs/resume.md")).toBe("first\n");
    expect(await restarted.executeApproval(pending.approvalId!)).toMatchObject({ ok: false, code: "APPROVAL_NOT_EXECUTABLE" });

    const resumable = checkpoints.prepareResume(checkpoints.load(checkpoint.id));
    expect(resumable.status).toBe("paused");
    expect(resumable.pendingApprovalId).toBeUndefined();
    expect(checkpoints.load(checkpoint.id).status).toBe("paused");
    expect(checkpoints.load(checkpoint.id).pendingApprovalId).toBeUndefined();
  });

  it("makes a rejected durable approval terminal instead of replaying the model", async () => {
    const harness = createWorkspaceHarness();
    const context = harness.context({ workspace: harness.workspace });
    const pending = await harness.runtime.invoke({
      context,
      toolCallId: "frozen-reject",
      modelName: "write_file",
      args: { path: "docs/rejected.md", content: "never\n" },
    });
    const checkpoints = new RunCheckpointStore(harness.store);
    const checkpoint: DurableRun = {
      ...run(harness.workspace), traceId: context.traceId, context, status: "waiting_approval", pendingApprovalId: pending.approvalId,
    };
    checkpoints.save(checkpoint);
    harness.store.decideApproval(pending.approvalId!, "reject", "reviewer");

    expect(() => checkpoints.prepareResume(checkpoints.load(checkpoint.id))).toThrowError(expect.objectContaining({ code: "RUN_APPROVAL_REJECTED" }));
    expect(checkpoints.load(checkpoint.id)).toMatchObject({ status: "failed" });
  });
});
