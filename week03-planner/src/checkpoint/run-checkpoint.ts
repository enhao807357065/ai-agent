import { cp, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { basename, dirname, join, resolve } from "node:path";

/** 运行归档是应用层的 durable state，不试图序列化 Pi 的 JS 调用栈或子进程。 */
export const CheckpointStatus = {
  RUNNING: "running",
  WAITING_FOR_APPROVAL: "waiting_for_approval",
  PAUSED: "paused",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
  NEEDS_REVIEW: "needs_review",
} as const;
export type CheckpointStatus = (typeof CheckpointStatus)[keyof typeof CheckpointStatus];

export interface CheckpointAction {
  readonly toolCallId: string;
  readonly toolName: string;
  readonly input: unknown;
  readonly output?: string;
  readonly status: "completed" | "failed" | "started" | "outcome_unknown" | "waiting_approval";
  readonly failureReason?: string;
  readonly retries: number;
  readonly decision?: string;
  /** 写动作在启动前记录的目标摘要；恢复时变化表示不能安全重放。 */
  readonly targetDigest?: string;
  /** 审批的规范化补丁/替换意图摘要，不含审计时可变的目标内容。 */
  readonly patchHash?: string;
  /** 写成功后相关目标文件的摘要；用于证明落盘后的 post-image。 */
  readonly postImageDigest?: string;
  /** 写成功后整个受控 workspace 的摘要。 */
  readonly workspaceDigestAfter?: string;
}

export interface RunCheckpointData {
  readonly runId: string;
  readonly task: string;
  readonly status: CheckpointStatus;
  readonly workspace: { readonly root: string; readonly digest: string; readonly initialDigest: string };
  readonly plan: unknown;
  readonly evidence: readonly unknown[];
  readonly messages: readonly unknown[];
  readonly actions: readonly CheckpointAction[];
  readonly runCount: number;
  readonly retryCount: number;
  readonly budgetConsumed: number;
  readonly nextDecision?: string;
  readonly pendingApproval?: {
    readonly actionId: string;
    readonly toolName: "modify_code" | "apply_patch";
    readonly parameters: unknown;
    readonly targetDigest: string;
    readonly fingerprint: string;
  };
  readonly humanDecisions: readonly { readonly actionId: string; readonly decision: "approved" | "rejected"; readonly fingerprint: string }[];
}

interface CheckpointEnvelope { readonly format: "week03-run-checkpoint"; readonly version: 1; readonly payload: RunCheckpointData; readonly checksum: string; }
export class CheckpointError extends Error { constructor(readonly code: string, message: string) { super(message); } }

export async function workspaceDigest(root: string): Promise<string> {
  const manifest: string[] = [];
  async function walk(path: string): Promise<void> {
    const entries = await (await import("node:fs/promises")).readdir(path, { withFileTypes: true });
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name, "en"))) {
      if (entry.name === ".git" || entry.name === "node_modules") continue;
      const target = join(path, entry.name);
      if (entry.isDirectory()) await walk(target);
      else if (entry.isFile()) manifest.push(`${target.slice(root.length + 1)}:${createHash("sha256").update(await readFile(target)).digest("hex")}`);
    }
  }
  await walk(resolve(root));
  return createHash("sha256").update(manifest.join("\n")).digest("hex");
}

/** 先落临时文件再 rename；若失败不会覆盖上一份有效 checkpoint。 */
export async function saveCheckpoint(path: string, payload: RunCheckpointData): Promise<void> {
  const canonical = JSON.stringify(payload);
  const envelope: CheckpointEnvelope = { format: "week03-run-checkpoint", version: 1, payload, checksum: createHash("sha256").update(canonical).digest("hex") };
  const target = resolve(path);
  const temp = join(dirname(target), `.${basename(target)}.${randomUUID()}.tmp`);
  try {
    await mkdir(dirname(target), { recursive: true });
    await writeFile(temp, JSON.stringify(envelope, null, 2), "utf8");
    await rename(temp, target);
  } catch (error) {
    throw new CheckpointError("CHECKPOINT_SAVE_FAILED", error instanceof Error ? error.message : "Checkpoint save failed.");
  }
}

export async function loadCheckpoint(path: string): Promise<RunCheckpointData> {
  let raw: unknown;
  try { raw = JSON.parse(await readFile(resolve(path), "utf8")); } catch { throw new CheckpointError("CHECKPOINT_INVALID", "Checkpoint is missing or is not valid JSON."); }
  if (!raw || typeof raw !== "object") throw new CheckpointError("CHECKPOINT_INVALID", "Checkpoint envelope is invalid.");
  const data = raw as Partial<CheckpointEnvelope>;
  if (data.format !== "week03-run-checkpoint" || data.version !== 1 || !data.payload || typeof data.checksum !== "string") throw new CheckpointError("CHECKPOINT_INVALID", "Checkpoint format/version is unsupported.");
  if (createHash("sha256").update(JSON.stringify(data.payload)).digest("hex") !== data.checksum) throw new CheckpointError("CHECKPOINT_CORRUPT", "Checkpoint checksum does not match its payload.");
  return data.payload;
}

/**
 * 恢复前的 fail-closed 检查。结果不明的写入和未完整结束的测试永远不会自动重放。
 * 调用方应在此检查通过后才构建业务对象和调用 Pi 的新一轮 loop。
 */
export async function inspectForResume(checkpoint: RunCheckpointData): Promise<{ readonly allowed: boolean; readonly reason?: string }> {
  if (checkpoint.status === CheckpointStatus.COMPLETED || checkpoint.status === CheckpointStatus.FAILED || checkpoint.status === CheckpointStatus.CANCELLED) return { allowed: false, reason: "RUN_TERMINAL" };
  if (checkpoint.status === CheckpointStatus.WAITING_FOR_APPROVAL) {
    return checkpoint.pendingApproval ? { allowed: false, reason: "APPROVAL_PENDING" } : { allowed: false, reason: "PENDING_APPROVAL_MISSING" };
  }
  try { await stat(checkpoint.workspace.root); } catch { return { allowed: false, reason: "WORKSPACE_MISSING" }; }
  const current = await workspaceDigest(checkpoint.workspace.root);
  if (checkpoint.actions.some((action) => action.status === "outcome_unknown")) return { allowed: false, reason: "UNKNOWN_ACTION_OUTCOME" };
  if (checkpoint.actions.some((action) => action.status === "started" && action.toolName === "run_tests")) return { allowed: false, reason: "TEST_RESULT_INCOMPLETE" };
  if (current !== checkpoint.workspace.digest) return { allowed: false, reason: "WORKSPACE_CHANGED" };
  return { allowed: true };
}

export function mergeResumedMessages(history: readonly unknown[] | undefined, newMessages: readonly unknown[]): readonly unknown[] {
  // Pi continuation 返回的只有本 invocation 新增消息；持久化必须累计。Plan snapshot 是派生上下文，不能作为历史事实反复保存。
  return [...(history ?? []), ...newMessages].filter((message) => !isPlanSnapshot(message));
}
function isPlanSnapshot(message: unknown): boolean {
  return !!message && typeof message === "object" && (message as { role?: unknown }).role === "user"
    && typeof (message as { content?: unknown }).content === "string"
    && (message as { content: string }).content.startsWith("[PLAN_STATE]");
}

/** R5：复制初始输入到新的独立目录，原 checkpoint 从不被覆写。 */
export async function createRetryWorkspace(initialWorkspace: string, parent: string): Promise<string> {
  const destination = join(resolve(parent), `retry-${randomUUID()}`);
  await cp(resolve(initialWorkspace), destination, { recursive: true, filter: (source) => !source.includes("node_modules") && !source.includes("/.git/") });
  return destination;
}
