import { contentText } from "@earendil-works/pi-ai";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { SourceWriteApprovalGate, fingerprintOf, patchHashOf, type ProposedSourceWrite } from "../approval/source-write-approval.js";
import { SourceWriteApprovalHook, expectedSourceWritePostImageDigest, sourceWriteTargetDigest } from "../approval/source-write-approval-hook.js";
import { CheckpointStatus, inspectForResume, loadCheckpoint, mergeResumedMessages, saveCheckpoint, workspaceDigest, type RunCheckpointData } from "../checkpoint/run-checkpoint.js";
import { createGatewayModelRuntime } from "../model/gateway-models.js";
import { CodeVersionTracker } from "../planning/code-version.js";
import { PlanStore, StepKind, StepKindVerifier } from "../planning/plan-store.js";
import { createRunExecutionContext } from "../runtime/execution-context.js";
import { ToolName, ToolRuntime, type ControlledToolResult } from "../runtime/tool-runtime.js";
import { createCliObserver } from "./cli-observer.js";
import { installSigintCancellation } from "./cli-cancellation.js";
import { parseCodebaseAgentCliOptions } from "./cli-options.js";
import { runCodebaseAgent, codebaseAgentSystemPrompt } from "./codebase-agent-run.js";
import { StopReason } from "./run-stop-center.js";
import { readTaskInput } from "./task-input.js";

/**
 * 正式入口使用 Evidence-first 动态计划。CLI 只把显式 flag 转成可信运行配置；
 * 不把 --repository / --state-dir 等控制面参数交给模型，也不由模型决定仓库根目录或自动审批策略。
 */
const defaultTask = "审计 login_demo 的 access token expiry 边界：[issuedAt, expiresAt)，now === expiresAt 必须拒绝。根据诊断事实动态扩展计划；若现有实现已满足要求，不得制造代码修改，改为执行真实测试和构建验证。";
const cli = parseCodebaseAgentCliOptions(process.argv.slice(2));
const controlRunId = cli.resumeRunId ?? cli.approveRunId ?? cli.rejectRunId;
const projectRoot = resolve(import.meta.dirname, "../..");
// --workspace 保留为旧版 state-dir 别名，不再隐式推导 `<workspace>/demo-app`。
const stateDir = resolve(cli.stateDir ?? cli.workspace ?? projectRoot);
const artifactsRoot = resolve(stateDir, "artifacts");
const resumedCheckpoint = controlRunId
  ? await loadCheckpoint(join(artifactsRoot, "runs", `${controlRunId}.json`))
  : undefined;
if (resumedCheckpoint && cli.resumeRunId) {
  const verdict = await inspectForResume(resumedCheckpoint);
  if (!verdict.allowed) throw new Error(`Cannot resume ${resumedCheckpoint.runId}: ${verdict.reason ?? "CHECKPOINT_REJECTED"}.`);
}
const task = resumedCheckpoint
  ? { value: resumedCheckpoint.task, source: "checkpoint" as const }
  : readTaskInput(cli.taskArguments, process.env.CODEBASE_AGENT_TASK, defaultTask);
const repositoryRoot = resolve(cli.repository ?? resumedCheckpoint?.workspace.root ?? resolve(projectRoot, "login_demo"));
const taskArtifactPath = "login-fix.md";
await stat(repositoryRoot);
await mkdir(artifactsRoot, { recursive: true });

// checkpoint 中的 root 是恢复身份的一部分；显式 --repository 也不能偷偷切换到另一仓库。
if (resumedCheckpoint && repositoryRoot !== resolve(resumedCheckpoint.workspace.root)) {
  throw new Error("Cannot resume with a repository different from the checkpoint workspace.");
}
const initialWorkspaceDigest = resumedCheckpoint?.workspace.initialDigest ?? await workspaceDigest(repositoryRoot);
let resumeMessages = resumedCheckpoint ? restoreMessages(resumedCheckpoint) : undefined;
let actionLedger = resumedCheckpoint?.actions ?? [];
let humanDecisions = resumedCheckpoint?.humanDecisions ?? [];
const runId = resumedCheckpoint?.runId ?? cli.runId ?? crypto.randomUUID();
const planId = resumedCheckpoint ? restoredPlanId(resumedCheckpoint) : `dynamic-${crypto.randomUUID()}`;
const codeVersion = new CodeVersionTracker();
const planStore = new PlanStore(() => codeVersion.current(), new StepKindVerifier());
if (resumedCheckpoint) planStore.restore(restoredPlan(resumedCheckpoint));
else planStore.create(planId, [{
  id: "diagnose-task",
  kind: StepKind.INVESTIGATION,
  title: "根据用户任务检查相关源码、配置与现有测试，确认根因、范围和可验证交付路径",
  successCriteria: ["引用真实 read_file 或 search_code 调用的 note Evidence", "明确后续需要实施还是仅验证"],
}], {
  goal: task.value,
  constraints: ["先诊断再扩展 Plan", "不得伪造变更、测试结果或 Evidence", "只使用服务端允许的受控工具"],
});
const executionContext = createRunExecutionContext({ projectRoot, repositoryRoot, artifactsRoot, taskArtifactPath });
const runtime = new ToolRuntime({ planStore, codeVersionTracker: codeVersion });
const approvalGate = new SourceWriteApprovalGate();

const sourceWriteApproval = new SourceWriteApprovalHook(approvalGate, repositoryRoot, { autoApprove: cli.autoApprove });
if (cli.approveRunId || cli.rejectRunId) {
  if (!resumedCheckpoint || resumedCheckpoint.status !== CheckpointStatus.WAITING_FOR_APPROVAL || !resumedCheckpoint.pendingApproval) {
    throw new Error("Approval command requires a waiting_for_approval checkpoint with a pending proposal.");
  }
  const proposal = proposalFromCheckpoint(resumedCheckpoint);
  const currentTargetDigest = await sourceWriteTargetDigest(repositoryRoot, proposal);
  if (currentTargetDigest !== proposal.targetDigest || fingerprintOf(proposal) !== resumedCheckpoint.pendingApproval.fingerprint) {
    throw new Error("Cannot decide approval: the frozen source-write proposal or target file digest changed.");
  }
  const checkpointPath = join(artifactsRoot, "runs", `${resumedCheckpoint.runId}.json`);
  if (cli.rejectRunId) {
    resumeMessages = replaceFrozenToolResult(resumeMessages!, rejectedToolResult(proposal));
    // 拒绝是人类对本次运行推进权的收回，不是让模型把 rejection 当普通 tool error 自行寻找替代路径。
    // 只落盘可恢复事实；后续重新规划必须由操作者显式 --resume 发起。
    const decisionCheckpoint = checkpointAfterDecision(resumedCheckpoint, CheckpointStatus.PAUSED, proposal, "rejected");
    actionLedger = decisionCheckpoint.actions;
    humanDecisions = decisionCheckpoint.humanDecisions;
    await saveCheckpoint(checkpointPath, { ...decisionCheckpoint, messages: resumeMessages, plan: planStore.get(planId), evidence: planStore.get(planId).evidence });
    console.log(`已拒绝冻结写操作：${proposal.actionId}。未执行任何 handler；Run 已暂停。`);
    console.log("当前 Plan、Evidence 与拒绝决定已存档。若希望 Agent 基于拒绝结果重新规划，请显式执行：");
    console.log(`npm start -- --state-dir ${stateDir} --resume ${resumedCheckpoint.runId} --pause-after 30`);
    process.exit(0);
  } else {
    const expectedPostImageDigest = await expectedSourceWritePostImageDigest(repositoryRoot, proposal);
    const result = await runtime.execute({ toolCallId: proposal.actionId, toolName: proposal.toolName, modelArguments: stripPlanStepId(proposal.parameters), context: executionContext });
    if (!result.success) throw new Error(`Approved source write did not execute successfully: ${result.code}.`);
    const postImageDigest = await sourceWriteTargetDigest(repositoryRoot, proposal);
    if (postImageDigest !== expectedPostImageDigest) throw new Error("Approved source write post-image digest does not match the frozen patch intent.");
    const planStepId = proposalPlanStepId(proposal.parameters);
    if (planStepId && result.evidenceDraft) planStore.recordToolEvidence(planId, planStepId, { ...result.evidenceDraft, toolCallId: proposal.actionId });
    const workspaceDigestAfter = await workspaceDigest(repositoryRoot);
    resumeMessages = replaceFrozenToolResult(resumeMessages!, approvedToolResult(proposal, result.content, result.artifactPath, result.evidenceDraft));
    const decisionCheckpoint = checkpointAfterDecision(resumedCheckpoint, CheckpointStatus.RUNNING, proposal, "approved", { postImageDigest, workspaceDigestAfter });
    actionLedger = decisionCheckpoint.actions;
    humanDecisions = decisionCheckpoint.humanDecisions;
    await saveCheckpoint(checkpointPath, {
      ...decisionCheckpoint,
      workspace: { ...resumedCheckpoint.workspace, digest: workspaceDigestAfter }, plan: planStore.get(planId), evidence: planStore.get(planId).evidence, messages: resumeMessages,
    });
    console.log(`已批准并执行冻结写操作：${proposal.actionId}。post-image 已核验；现在继续同一 Run。`);
  }
}

const modelRuntime = createGatewayModelRuntime();
const controller = new AbortController();
const removeSigintCancellation = installSigintCancellation(controller);
const maxTurns = cli.pauseAfter ?? 50;
const observer = createCliObserver(planStore, planId);

console.log(`\n任务：${task.value}`);
console.log(`运行：${runId}  仓库：${repositoryRoot}  状态目录：${stateDir}${resumedCheckpoint ? "  模式：resume" : ""}`);
const result = await runCodebaseAgent({
  runId,
  model: modelRuntime.model,
  streamFn: (_model, context, options) => modelRuntime.stream(context, options),
  signal: controller.signal,
  runtime,
  executionContext,
  planStore,
  planId,
  sourceWriteApproval,
  maxTurns,
  completion: { kind: "fix", requiredPlanId: planId },
  systemPrompt: [
    codebaseAgentSystemPrompt,
    `本次正式入口使用 Evidence-first 动态计划：初始只有 diagnose-task，不预设缺陷存在。最终交付说明必须写入 ${taskArtifactPath}。诊断完成后必须用 revise_plan 基于真实 Evidence additions 追加实现+验证或仅验证分支。对用户可见的简短进度说明必须使用中文；不要输出隐藏推理、原始工具参数或工具结果全文。`,
  ].join("\n"),
  userTask: task.value,
  ...(resumeMessages ? { resumeMessages } : {}),
  onTextDelta: observer.onTextDelta,
  onLog: observer.onLog,
}).finally(removeSigintCancellation);
observer.flushText();

// Pi 原生 Loop 结束后才发布完整轮次 checkpoint；若 --pause-after 命中预算，语义改为 paused，
// 但不把未完成计划伪装为成功。存档失败直接抛出，调用者仍可保留上一份有效 checkpoint。
const status = result.state.stopReason === StopReason.WAITING_FOR_APPROVAL
  ? CheckpointStatus.WAITING_FOR_APPROVAL
  : cli.pauseAfter !== undefined && result.state.stopReason === StopReason.MAX_TURNS_REACHED
  ? CheckpointStatus.PAUSED
  : result.state.stopReason === "cancelled" ? CheckpointStatus.CANCELLED
    : result.state.stopReason === "completion_contract_satisfied" ? CheckpointStatus.COMPLETED
      : result.state.stopReason === "model_error" ? CheckpointStatus.FAILED : CheckpointStatus.RUNNING;
const digest = await workspaceDigest(repositoryRoot);
const checkpointPath = join(artifactsRoot, "runs", `${runId}.json`);
console.log(`\n存档中：状态=${status}，轮次=${result.state.turnCount}，路径=${checkpointPath}`);
await saveCheckpoint(checkpointPath, {
  runId,
  task: task.value,
  status,
  workspace: { root: repositoryRoot, digest, initialDigest: initialWorkspaceDigest },
  plan: planStore.get(planId),
  evidence: planStore.get(planId).evidence,
  messages: mergeResumedMessages(resumeMessages, result.messages),
  actions: sourceWriteApproval.pendingProposal() ? [...actionLedger, ...pendingAction(sourceWriteApproval.pendingProposal())] : actionLedger,
  runCount: result.state.turnCount,
  retryCount: 0,
  budgetConsumed: result.state.turnCount,
  nextDecision: status === CheckpointStatus.WAITING_FOR_APPROVAL ? "Await an explicit operator approve/reject command for the frozen source write." : status === CheckpointStatus.PAUSED ? "Resume from the persisted plan state after operator review." : undefined,
  ...(status === CheckpointStatus.WAITING_FOR_APPROVAL ? { pendingApproval: checkpointPendingApproval(sourceWriteApproval.pendingProposal()) } : {}),
  humanDecisions: [...humanDecisions, ...approvalGate.decisions()],
});
console.log(`存档完成：${checkpointPath}`);

process.stdout.write("\n");
console.log(`结果：runId=${result.state.runId}，轮次=${result.state.turnCount}，停止原因=${result.state.stopReason ?? "none"}，checkpoint=${status}`);
if (status === CheckpointStatus.WAITING_FOR_APPROVAL) {
  const pending = sourceWriteApproval.pendingProposal();
  if (!pending) throw new Error("waiting_for_approval requires a frozen source-write proposal.");
  console.log("\n等待人工审核：源码尚未写入。请检查本轮 Agent 说明与目标文件后，复制其中一条命令：");
  console.log(`批准（执行冻结的精确写操作）：\nnpm start -- --state-dir ${stateDir} --approve ${runId}`);
  console.log(`拒绝（不执行写操作）：\nnpm start -- --state-dir ${stateDir} --reject ${runId}`);
}
const finalAssistant = [...result.messages].reverse().find((message) => message.role === "assistant");
if (finalAssistant?.role === "assistant") { const visibleText = contentText(finalAssistant.content); if (visibleText.length > 0) console.log(`\nFinal:\n${visibleText}`); }

/** checkpoint 是不可信 JSON；恢复前先在 CLI 将它收窄为本项目的 Plan 聚合契约。 */
function restoredPlan(checkpoint: RunCheckpointData): ReturnType<PlanStore["get"]> {
  if (!checkpoint.plan || typeof checkpoint.plan !== "object" || typeof (checkpoint.plan as { id?: unknown }).id !== "string") {
    throw new Error("Cannot resume: checkpoint plan is invalid.");
  }
  return checkpoint.plan as ReturnType<PlanStore["get"]>;
}
function restoredPlanId(checkpoint: RunCheckpointData): string { return restoredPlan(checkpoint).id; }

function proposalFromCheckpoint(checkpoint: RunCheckpointData): ProposedSourceWrite {
  const pending = checkpoint.pendingApproval;
  if (!pending) throw new Error("Checkpoint has no pending approval.");
  return { actionId: pending.actionId, toolName: pending.toolName, parameters: pending.parameters, targetDigest: pending.targetDigest };
}
function checkpointPendingApproval(proposal: ProposedSourceWrite | undefined): NonNullable<RunCheckpointData["pendingApproval"]> {
  if (!proposal) throw new Error("Missing frozen source-write proposal.");
  return { actionId: proposal.actionId, toolName: proposal.toolName, parameters: proposal.parameters, targetDigest: proposal.targetDigest, fingerprint: fingerprintOf(proposal) };
}
function pendingAction(proposal: ProposedSourceWrite | undefined): RunCheckpointData["actions"] {
  return proposal ? [{ toolCallId: proposal.actionId, toolName: proposal.toolName, input: proposal.parameters, status: "waiting_approval", retries: 0, targetDigest: proposal.targetDigest, patchHash: patchHashOf(proposal) }] : [];
}
function checkpointAfterDecision(
  checkpoint: RunCheckpointData,
  status: CheckpointStatus,
  proposal: ProposedSourceWrite,
  decision: "approved" | "rejected",
  audit: { readonly postImageDigest: string; readonly workspaceDigestAfter: string } | undefined = undefined,
): RunCheckpointData {
  return {
    ...checkpoint,
    status,
    pendingApproval: undefined,
    actions: [{
      toolCallId: proposal.actionId, toolName: proposal.toolName, input: proposal.parameters,
      status: decision === "approved" ? "completed" : "failed",
      failureReason: decision === "rejected" ? "Rejected by operator before handler execution." : undefined,
      retries: 0, decision, targetDigest: proposal.targetDigest, patchHash: patchHashOf(proposal),
      ...(audit === undefined ? {} : { postImageDigest: audit.postImageDigest, workspaceDigestAfter: audit.workspaceDigestAfter }),
    }],
    humanDecisions: [...checkpoint.humanDecisions, { actionId: proposal.actionId, decision, fingerprint: fingerprintOf(proposal) }],
    nextDecision: decision === "rejected" ? "Resume only if an operator wants the Agent to handle the rejection." : "Resume from the exact successful source-write result.",
  };
}
function proposalPlanStepId(parameters: unknown): string | undefined {
  const value = parameters && typeof parameters === "object" ? (parameters as Record<string, unknown>).planStepId : undefined;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
function stripPlanStepId(parameters: unknown): Record<string, unknown> {
  if (!parameters || typeof parameters !== "object") throw new Error("Frozen approval parameters are invalid.");
  const { planStepId: _ignored, ...runtimeArguments } = parameters as Record<string, unknown>;
  return runtimeArguments;
}
function approvedToolResult(proposal: ProposedSourceWrite, content: string, artifactPath: string | undefined, evidenceDraft: ControlledToolResult["evidenceDraft"]): import("@earendil-works/pi-agent-core").AgentMessage {
  return {
    role: "toolResult", toolCallId: proposal.actionId, toolName: proposal.toolName,
    content: [{ type: "text", text: content }],
    details: { code: "ok", ...(artifactPath === undefined ? {} : { artifactPath }), ...(evidenceDraft === undefined ? {} : { evidenceDraft }) },
    isError: false, timestamp: Date.now(),
  } as import("@earendil-works/pi-agent-core").AgentMessage;
}
function rejectedToolResult(proposal: ProposedSourceWrite): import("@earendil-works/pi-agent-core").AgentMessage {
  return {
    role: "toolResult", toolCallId: proposal.actionId, toolName: proposal.toolName,
    content: [{ type: "text", text: "[APPROVAL_REJECTED] The operator rejected this exact source write; no handler was executed. Re-plan without assuming it was applied." }],
    details: {}, isError: true, timestamp: Date.now(),
  } as import("@earendil-works/pi-agent-core").AgentMessage;
}
function replaceFrozenToolResult(messages: readonly import("@earendil-works/pi-agent-core").AgentMessage[], approved: import("@earendil-works/pi-agent-core").AgentMessage): import("@earendil-works/pi-agent-core").AgentMessage[] {
  const approvedCallId = (approved as { toolCallId?: string }).toolCallId;
  const index = messages.findLastIndex((message) => message.role === "toolResult" && (message as { toolCallId: string }).toolCallId === approvedCallId);
  if (index < 0) throw new Error("Frozen approval tool result is absent from checkpoint history.");
  return [...messages.slice(0, index), approved, ...messages.slice(index + 1)];
}
function restoreMessages(checkpoint: RunCheckpointData): import("@earendil-works/pi-agent-core").AgentMessage[] {
  if (!Array.isArray(checkpoint.messages) || checkpoint.messages.length === 0) {
    throw new Error("Cannot resume: checkpoint has no Pi message history.");
  }
  const messages = checkpoint.messages as import("@earendil-works/pi-agent-core").AgentMessage[];
  const last = messages.at(-1);
  if (!last || (last.role !== "user" && last.role !== "toolResult")) {
    throw new Error("Cannot resume: checkpoint history must end with a user or tool result message.");
  }
  return messages;
}
