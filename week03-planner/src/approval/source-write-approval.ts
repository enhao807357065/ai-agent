import { createHash } from "node:crypto";

export interface ProposedSourceWrite {
  readonly actionId: string;
  readonly toolName: "modify_code" | "apply_patch";
  readonly parameters: unknown;
  /** 目标相关文件内容摘要由调用方在执行前提供，避免旧批准覆盖后来文件状态。 */
  readonly targetDigest: string;
}
export interface ApprovalRecord { readonly actionId: string; readonly fingerprint: string; readonly decision: "approved" | "rejected"; }

/**
 * 人工接管只保存“某一次被冻结的源码写入”授权。它不是工具级白名单：参数或目标摘要变化
 * 会生成新 fingerprint，因此重启、相同工具名或看似相同的下一次调用都不会继承授权。
 */
export class SourceWriteApprovalGate {
  #pending = new Map<string, ProposedSourceWrite>();
  #decisions = new Map<string, ApprovalRecord>();

  propose(proposal: ProposedSourceWrite): ApprovalRecord | undefined {
    const fingerprint = fingerprintOf(proposal);
    const previous = this.#decisions.get(proposal.actionId);
    if (previous?.decision === "approved" && previous.fingerprint === fingerprint) return previous;
    this.#pending.set(proposal.actionId, proposal);
    return undefined;
  }
  decide(actionId: string, decision: "approved" | "rejected"): ApprovalRecord {
    const pending = this.#pending.get(actionId);
    if (!pending) throw new Error("APPROVAL_ACTION_NOT_PENDING");
    const record: ApprovalRecord = { actionId, fingerprint: fingerprintOf(pending), decision };
    this.#pending.delete(actionId); this.#decisions.set(actionId, record);
    return record;
  }
  authorize(proposal: ProposedSourceWrite): boolean {
    const record = this.#decisions.get(proposal.actionId);
    return record?.decision === "approved" && record.fingerprint === fingerprintOf(proposal);
  }
  pending(actionId: string): ProposedSourceWrite | undefined { return this.#pending.get(actionId); }
  /** 单一 Run 同时只允许一个冻结写动作进入等待态，避免多 proposal 竞态。 */
  pendingProposal(): ProposedSourceWrite | undefined { return this.#pending.values().next().value; }
  decisions(): readonly ApprovalRecord[] { return [...this.#decisions.values()]; }
}
export function fingerprintOf(proposal: Omit<ProposedSourceWrite, "actionId"> | ProposedSourceWrite): string {
  return createHash("sha256").update(JSON.stringify(canonicalize({ toolName: proposal.toolName, parameters: proposal.parameters, targetDigest: proposal.targetDigest }))).digest("hex");
}
export function patchHashOf(proposal: Pick<ProposedSourceWrite, "toolName" | "parameters"> | ProposedSourceWrite): string {
  const parameters = proposal.parameters && typeof proposal.parameters === "object" ? proposal.parameters as Record<string, unknown> : {};
  const intent = proposal.toolName === "modify_code"
    ? { toolName: proposal.toolName, path: parameters.path, oldText: parameters.oldText, newText: parameters.newText }
    : { toolName: proposal.toolName, patches: parameters.patches };
  return createHash("sha256").update(JSON.stringify(canonicalize(intent))).digest("hex");
}
function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b, "en"))
      .map(([key, nested]) => [key, canonicalize(nested)]));
  }
  return value;
}
