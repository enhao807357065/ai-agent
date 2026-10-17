import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { relative, resolve, sep } from "node:path";
import type { BeforeToolCallContext, BeforeToolCallResult } from "@earendil-works/pi-agent-core";
import { SourceWriteApprovalGate, type ProposedSourceWrite } from "../approval/source-write-approval.js";
import { ToolName } from "../runtime/tool-runtime.js";

/**
 * Pi Hook 只做“写前审批”决策，绝不直接执行工具或修改计划。批准后的真实调用仍由
 * Pi adapter -> ToolRuntime 的既有唯一执行入口完成。targetDigest 在提案时读取；恢复/重试
 * 若文件内容不同会形成新 fingerprint，旧审批不可能放行。
 */
export interface SourceWriteApprovalHookOptions {
  /** 仅由显式 CLI 教学开关注入；仍会逐次冻结 toolCallId、参数与目标摘要。 */
  readonly autoApprove?: boolean;
}

export async function sourceWriteTargetDigest(repositoryRoot: string, proposal: Pick<ProposedSourceWrite, "toolName" | "parameters">): Promise<string> {
  const args = proposal.parameters as Record<string, unknown>;
  const files = proposal.toolName === ToolName.MODIFY_CODE ? [String(args.path)] : ((args.patches as Array<{ path: string }> | undefined) ?? []).map((patch) => patch.path);
  const root = resolve(repositoryRoot);
  const content = await Promise.all(files.sort().map(async (path) => {
    const target = resolve(root, path);
    const rel = relative(root, target);
    if (rel === ".." || rel.startsWith(`..${sep}`) || rel.includes(`..${sep}`)) throw new Error("APPROVAL_PATH_REJECTED");
    return `${path}:${createHash("sha256").update(await readFile(target)).digest("hex")}`;
  }));
  return createHash("sha256").update(content.join("\n")).digest("hex");
}

export async function expectedSourceWritePostImageDigest(repositoryRoot: string, proposal: Pick<ProposedSourceWrite, "toolName" | "parameters">): Promise<string> {
  const args = proposal.parameters as Record<string, unknown>;
  const replacements = proposal.toolName === ToolName.MODIFY_CODE
    ? [{ path: String(args.path), oldText: String(args.oldText), newText: String(args.newText) }]
    : ((args.patches as Array<{ path: string; oldText: string; newText: string }> | undefined) ?? []);
  const root = resolve(repositoryRoot);
  const expected = await Promise.all(replacements.sort((a, b) => a.path.localeCompare(b.path, "en")).map(async (patch) => {
    const target = approvalTargetPath(root, patch.path);
    const before = await readFile(target, "utf8");
    if (before.split(patch.oldText).length - 1 !== 1) throw new Error("APPROVAL_POST_IMAGE_PRECONDITION_FAILED");
    return `${patch.path}:${createHash("sha256").update(before.replace(patch.oldText, patch.newText)).digest("hex")}`;
  }));
  return createHash("sha256").update(expected.join("\n")).digest("hex");
}
function approvalTargetPath(root: string, path: string): string {
  const target = resolve(root, path);
  const rel = relative(root, target);
  if (rel === ".." || rel.startsWith(`..${sep}`) || rel.includes(`..${sep}`)) throw new Error("APPROVAL_PATH_REJECTED");
  return target;
}

export class SourceWriteApprovalHook {
  constructor(
    private readonly gate: SourceWriteApprovalGate,
    private readonly repositoryRoot: string,
    private readonly options: SourceWriteApprovalHookOptions = {},
  ) {}
  async beforeToolCall(context: BeforeToolCallContext): Promise<BeforeToolCallResult | undefined> {
    if (context.toolCall.name !== ToolName.MODIFY_CODE && context.toolCall.name !== ToolName.APPLY_PATCH) return undefined;
    const proposal = await this.#proposal(context);
    if (this.gate.authorize(proposal)) return undefined;
    const existing = this.gate.pending(proposal.actionId);
    if (!existing) this.gate.propose(proposal);
    // autoApprove 只适用于本次由 CLI 显式开启的、已冻结的具体 action；不是跨重启或跨调用的授权。
    if (this.options.autoApprove) { this.gate.decide(proposal.actionId, "approved"); return undefined; }
    // terminate 让 Pi 在本轮结束后停止，不把审批拒绝作为普通 tool error 交给模型继续规划。
    return { block: true, terminate: true, reason: `[APPROVAL_REQUIRED] Source write ${proposal.actionId} is frozen for human review; no handler was executed.` };
  }
  approve(actionId: string) { return this.gate.decide(actionId, "approved"); }
  reject(actionId: string) { return this.gate.decide(actionId, "rejected"); }
  pendingProposal(): ProposedSourceWrite | undefined { return this.gate.pendingProposal(); }
  async #proposal(context: BeforeToolCallContext): Promise<ProposedSourceWrite> {
    const args = context.args as Record<string, unknown>;
    const files = context.toolCall.name === ToolName.MODIFY_CODE ? [String(args.path)] : ((args.patches as Array<{ path: string }> | undefined) ?? []).map((patch) => patch.path);
    const root = resolve(this.repositoryRoot);
    const content = await Promise.all(files.sort().map(async (path) => {
      const target = resolve(root, path);
      const rel = relative(root, target);
      if (rel === ".." || rel.startsWith(`..${sep}`) || rel.includes(`..${sep}`)) throw new Error("APPROVAL_PATH_REJECTED");
      return `${path}:${createHash("sha256").update(await readFile(target)).digest("hex")}`;
    }));
    const toolName = context.toolCall.name === ToolName.MODIFY_CODE ? ToolName.MODIFY_CODE : ToolName.APPLY_PATCH;
    return { actionId: context.toolCall.id, toolName, parameters: context.args, targetDigest: createHash("sha256").update(content.join("\n")).digest("hex") };
  }
}
