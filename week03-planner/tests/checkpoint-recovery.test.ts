import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { SourceWriteApprovalGate } from "../src/approval/source-write-approval.js";
import { CheckpointStatus, createRetryWorkspace, inspectForResume, loadCheckpoint, mergeResumedMessages, saveCheckpoint, workspaceDigest, type RunCheckpointData } from "../src/checkpoint/run-checkpoint.js";

const temporary: string[] = [];
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))); });
async function fixture() { const root = await mkdtemp(join(tmpdir(), "week03-checkpoint-")); temporary.push(root); const workspace = join(root, "repo"); await mkdir(workspace); await writeFile(join(workspace, "token.ts"), "export const expires = false;\n"); return { root, workspace }; }
async function checkpoint(workspace: string, overrides: Partial<RunCheckpointData> = {}): Promise<RunCheckpointData> { const digest = await workspaceDigest(workspace); return { runId: "run-1", task: "fix login expiry", status: CheckpointStatus.PAUSED, workspace: { root: workspace, digest, initialDigest: digest }, plan: { id: "plan" }, evidence: [{ id: "diagnosis" }], messages: [{ role: "user", content: "task" }], actions: [], runCount: 2, retryCount: 3, budgetConsumed: 7, humanDecisions: [], ...overrides }; }

describe("durable checkpoint and manual source-write approval", () => {
  it("A1 persists plan, diagnosis/evidence and counters and permits a clean resume", async () => {
    const { root, workspace } = await fixture(); const data = await checkpoint(workspace); const path = join(root, "run.json");
    await saveCheckpoint(path, data); const restored = await loadCheckpoint(path);
    expect(restored.plan).toEqual({ id: "plan" }); expect(restored.evidence).toEqual([{ id: "diagnosis" }]); expect(restored.runCount).toBe(2); expect(restored.budgetConsumed).toBe(7);
    await expect(inspectForResume(restored)).resolves.toEqual({ allowed: true });
  });
  it("A1b accumulates prior Pi protocol history across continuations but drops derived plan snapshots", () => {
    const history = [{ role: "user", content: "original diagnosis", timestamp: 1 }, { role: "toolResult", toolCallId: "read-1", toolName: "read_file", content: [], details: {}, isError: false, timestamp: 2 }, { role: "user", content: "[PLAN_STATE] stale", timestamp: 3 }];
    const next = [{ role: "assistant", content: [], timestamp: 4 }, { role: "toolResult", toolCallId: "test-1", toolName: "run_tests", content: [], details: {}, isError: false, timestamp: 5 }];
    expect(mergeResumedMessages(history, next)).toEqual([history[0], history[1], next[0], next[1]]);
  });
  it("keeps a waiting approval checkpoint out of autonomous resume", async () => {
    const { workspace } = await fixture(); const data = await checkpoint(workspace, {
      status: CheckpointStatus.WAITING_FOR_APPROVAL,
      pendingApproval: { actionId: "write-1", toolName: "modify_code", parameters: { path: "token.ts", oldText: "false", newText: "true" }, targetDigest: "digest", fingerprint: "fingerprint" },
      actions: [{ toolCallId: "write-1", toolName: "modify_code", input: {}, status: "waiting_approval", retries: 0, targetDigest: "digest" }],
    });
    await expect(inspectForResume(data)).resolves.toEqual({ allowed: false, reason: "APPROVAL_PENDING" });
  });

  it("permits an explicit resume after a persisted rejection, without treating rejection as a terminal run", async () => {
    const { workspace } = await fixture(); const data = await checkpoint(workspace, {
      status: CheckpointStatus.PAUSED,
      actions: [{ toolCallId: "write-1", toolName: "modify_code", input: { path: "token.ts" }, status: "failed", failureReason: "Rejected by operator before handler execution.", retries: 0, decision: "rejected", targetDigest: "digest", patchHash: "patch" }],
      humanDecisions: [{ actionId: "write-1", decision: "rejected", fingerprint: "fingerprint" }],
      nextDecision: "Resume only if an operator wants the Agent to handle the rejection.",
    });
    await expect(inspectForResume(data)).resolves.toEqual({ allowed: true });
  });

  it("A2 blocks an uncertain source write after an abnormal exit instead of replaying it", async () => {
    const { workspace } = await fixture(); const data = await checkpoint(workspace, { actions: [{ toolCallId: "write-1", toolName: "modify_code", input: {}, status: "outcome_unknown", retries: 0 }] });
    await writeFile(join(workspace, "token.ts"), "export const expires = true;\n");
    await expect(inspectForResume(data)).resolves.toEqual({ allowed: false, reason: "UNKNOWN_ACTION_OUTCOME" });
  });
  it("A3 treats an interrupted test as incomplete, never as passed", async () => {
    const { workspace } = await fixture(); const data = await checkpoint(workspace, { actions: [{ toolCallId: "test-1", toolName: "run_tests", input: { script: "test" }, status: "started", retries: 0 }] });
    await expect(inspectForResume(data)).resolves.toEqual({ allowed: false, reason: "TEST_RESULT_INCOMPLETE" });
  });
  it("A4/A5 requires an exact approval; rejection and changed parameters never execute under the old decision", () => {
    const gate = new SourceWriteApprovalGate(); const original = { actionId: "patch-1", toolName: "modify_code" as const, parameters: { path: "token.ts", oldText: "false", newText: "true" }, targetDigest: "v1" };
    expect(gate.propose(original)).toBeUndefined(); expect(gate.authorize(original)).toBe(false); gate.decide("patch-1", "rejected"); expect(gate.authorize(original)).toBe(false);
    gate.propose({ ...original, actionId: "patch-2" }); gate.decide("patch-2", "approved"); expect(gate.authorize({ ...original, actionId: "patch-2" })).toBe(true);
    expect(gate.authorize({ ...original, actionId: "patch-2", parameters: { ...original.parameters, newText: "strict" } })).toBe(false);
    expect(gate.authorize({ ...original, actionId: "patch-2", targetDigest: "v2" })).toBe(false);
  });
  it("A6 rejects corrupt checkpoints, absent workspaces, and terminal runs", async () => {
    const { root, workspace } = await fixture(); const path = join(root, "corrupt.json"); await writeFile(path, "{ nope"); await expect(loadCheckpoint(path)).rejects.toMatchObject({ code: "CHECKPOINT_INVALID" });
    const missingWorkspace = await checkpoint(workspace); const terminal = await checkpoint(workspace, { status: CheckpointStatus.COMPLETED }); await rm(workspace, { recursive: true });
    await expect(inspectForResume(missingWorkspace)).resolves.toEqual({ allowed: false, reason: "WORKSPACE_MISSING" });
    await expect(inspectForResume(terminal)).resolves.toEqual({ allowed: false, reason: "RUN_TERMINAL" });
  });
  it("A7 preserves failure/retry/budget records through checkpoint restore", async () => {
    const { root, workspace } = await fixture(); const path = join(root, "paused.json"); const data = await checkpoint(workspace, { status: CheckpointStatus.PAUSED, retryCount: 4, budgetConsumed: 11, actions: [{ toolCallId: "test-3", toolName: "run_tests", input: {}, status: "failed", failureReason: "exit 1", retries: 3, decision: "pause for operator" }] }); await saveCheckpoint(path, data);
    const restored = await loadCheckpoint(path); expect(restored.retryCount).toBe(4); expect(restored.budgetConsumed).toBe(11); expect(restored.actions[0]).toMatchObject({ status: "failed", retries: 3, failureReason: "exit 1" });
  });
  it("A8 forks an independent retry workspace and leaves original run inputs unchanged", async () => {
    const { root, workspace } = await fixture(); const retry = await createRetryWorkspace(workspace, root); await writeFile(join(retry, "token.ts"), "export const expires = true;\n");
    await expect(readFile(join(workspace, "token.ts"), "utf8")).resolves.toContain("false"); await expect(readFile(join(retry, "token.ts"), "utf8")).resolves.toContain("true"); expect(retry).not.toBe(workspace);
  });
});
