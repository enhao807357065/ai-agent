import type {
  AfterToolCallContext,
  BeforeToolCallContext,
  BeforeToolCallResult,
  ShouldStopAfterTurnContext,
} from "@earendil-works/pi-agent-core";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { EvidenceKind, PlanStore, StepKind, StepStatus, type PlanStep } from "../planning/plan-store.js";
import type { RunPlanContext } from "../planning/run-plan-context.js";
import { ToolName, ToolResultCode } from "../runtime/tool-runtime.js";
import type { RuntimeToolDetails } from "../tools/pi-runtime-tools.js";

/** 真实 Run 结束原因；completed 只能由完成契约满足产生。 */
export const StopReason = {
  REPEATED_ACTION: "repeated_action",
  MAX_TURNS_REACHED: "max_turns_reached",
  COMPLETION_CONTRACT_SATISFIED: "completion_contract_satisfied",
  /** 写动作已冻结；只能由 CLI 的 approve/reject 控制面推进，模型不可自行继续。 */
  WAITING_FOR_APPROVAL: "waiting_for_approval",
} as const;
export type StopReason = (typeof StopReason)[keyof typeof StopReason];
export const StopDecisionCode = { REPEATED_ACTION: "repeated_action", FINAL_REJECTED: "FINAL_REJECTED" } as const;

/** 原代码理解任务的完成契约。 */
export interface CodeUnderstandingCompletionContract {
  readonly kind: "code_understanding";
  readonly minimumReadFiles: number;
  readonly requiredArtifactPath: string;
}
/** 修复交付的完成契约：证据由 afterToolCall 从真实受控工具结果记录，而非模型自述。 */
export interface FixCompletionContract {
  readonly kind: "fix";
  /** 静态绑定用于旧教学调用；LLM-first Run 则从 RunPlanContext 读取活跃 Plan。 */
  readonly requiredPlanId?: string;
}
/** 兼容原 lesson 的输入形状；未显式 kind 时按 code_understanding 解释。 */
export type CompletionContract = CodeUnderstandingCompletionContract | FixCompletionContract;
export type CompletionRequirements = Omit<CodeUnderstandingCompletionContract, "kind">;

export interface RunStopCenterOptions {
  readonly runId: string;
  readonly maxTurns: number;
  readonly completion: CompletionContract | CompletionRequirements;
  /** 可选教学观测输出：仅输出状态、工具名、ID 和计划摘要，不输出模型全文或工具结果正文。 */
  readonly onLog?: (message: string) => void;
  /** 可选后才启用 Planner 对写工具的 step 绑定，保持原代码理解任务可运行。 */
  readonly planStore?: PlanStore;
  readonly planId?: string;
  /** LLM-first Run 的活跃 Plan 指针；提供后优先于静态 planId。 */
  readonly runPlanContext?: RunPlanContext;
  /** 失败的 run_tests 会被 Pi 映射为 error ToolResult；通过该只读查询取回 Runtime 已记录的真实验证事实。 */
  readonly getVerificationOutcome?: (toolCallId: string) => { readonly scope: string; readonly testedDigest: string; readonly passed: boolean; readonly failureCode?: string } | undefined;
}
export interface RunStopState {
  readonly runId: string; readonly currentTurn: number; readonly maxTurns: number;
  readonly stopReason?: StopReason; readonly actionCounts: Readonly<Record<string, number>>;
  readonly readSourceFiles: readonly string[]; readonly writtenArtifacts: readonly string[];
  readonly pendingFollowUps: readonly AgentMessage[];
}

/**
 * Run 级治理中心：不执行工具、不调用模型。
 * Hook 时序：beforeToolCall 守卫重复/planStepId → Adapter/Runtime 执行 → afterToolCall 记录受控证据
 * → shouldStopAfterTurn 只根据 CompletionContract 决定是否结束。
 */
export class RunStopCenter {
  readonly #actionCounts = new Map<string, number>();
  readonly #readSourceFiles = new Set<string>();
  readonly #writtenArtifacts = new Set<string>();
  readonly #pendingFollowUps: AgentMessage[] = [];
  readonly #completion: CompletionContract;
  #currentTurn = 0; #stopReason: StopReason | undefined;

  constructor(private readonly options: RunStopCenterOptions) {
    if (!Number.isInteger(options.maxTurns) || options.maxTurns < 1) throw new Error("maxTurns must be a positive integer.");
    this.#completion = normalizeContract(options.completion);
    if ((options.planStore === undefined) !== (options.planId === undefined) && !options.runPlanContext) {
      throw new Error("planStore and planId must be provided together unless runPlanContext owns activation.");
    }
    if (options.runPlanContext && !options.planStore) throw new Error("runPlanContext requires planStore.");
  }

  #activePlanId(): string | undefined {
    return this.options.runPlanContext?.activePlanId() ?? this.options.planId;
  }

  // 检查计划依赖、重复动作和当前是否允许继续
  beforeToolCall(context: BeforeToolCallContext): BeforeToolCallResult | undefined {
    if (this.#stopReason) {
      this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=${this.#stopReason}`);
      return blockedResult(this.#stopReason);
    }
    const actionKey = actionFingerprint(context.toolCall.name, context.args);
    const count = (this.#actionCounts.get(actionKey) ?? 0) + 1;
    this.#actionCounts.set(actionKey, count);
    this.#log(`tool.requested name=${context.toolCall.name} callId=${context.toolCall.id} repeat=${count}`);
    if (count >= 3) {
      this.#setStopReason(StopReason.REPEATED_ACTION);
      this.#log(`loop.stop reason=${StopReason.REPEATED_ACTION} detail=third_identical_tool_action`);
      return { block: true, terminate: true, reason: `[${StopDecisionCode.REPEATED_ACTION}] The same tool action cannot be executed more than twice in this run.` };
    }

    // planStepId 是模型可见的治理 metadata。只有写入类工具必须携带，并且必须指向 running 步骤。
    if (requiresPlanStepId(context.toolCall.name) && this.options.planStore) {
      const activePlanId = this.#activePlanId();
      if (!activePlanId) {
        this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=PLAN_REQUIRED`);
        return { block: true, reason: "[PLAN_REQUIRED] Create and activate a plan before executing governed tools." };
      }
      const stepId = asNonEmptyString(context.args, "planStepId");
      if (!stepId) {
        this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=PLAN_STEP_REQUIRED`);
        return { block: true, reason: "[PLAN_STEP_REQUIRED] This write tool requires planStepId." };
      }
      try {
        const step = this.options.planStore.get(activePlanId).steps.find((item) => item.id === stepId);
        if (!step || step.status !== StepStatus.RUNNING) {
          this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=PLAN_STEP_NOT_RUNNING stepId=${stepId}`);
          return { block: true, reason: "[PLAN_STEP_NOT_RUNNING] planStepId must refer to a running plan step." };
        }
        const requiredKind = requiredStepKind(context.toolCall.name);
        if (requiredKind && step.kind !== requiredKind) {
          this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=PLAN_STEP_KIND_MISMATCH stepId=${stepId} stepKind=${step.kind} requiredKind=${requiredKind}`);
          return {
            block: true,
            reason: `[PLAN_STEP_KIND_MISMATCH] ${context.toolCall.name} requires a running ${requiredKind} step; ${stepId} is ${step.kind}.`,
          };
        }
        this.#log(`plan.step.bound stepId=${stepId} kind=${step.kind} status=${step.status} tool=${context.toolCall.name}`);
      } catch {
        this.#log(`tool.blocked name=${context.toolCall.name} callId=${context.toolCall.id} reason=PLAN_NOT_FOUND`);
        return { block: true, reason: "[PLAN_NOT_FOUND] Plan state is unavailable." };
      }
    }
    return undefined;
  }

  /** 审批 Hook 发现冻结 proposal 时调用；StopCenter 是唯一 Run 停止事实所有者。 */
  waitForApproval(): void {
    this.#setStopReason(StopReason.WAITING_FOR_APPROVAL);
    this.#log(`loop.stop reason=${StopReason.WAITING_FOR_APPROVAL} detail=source_write_pending_human_decision`);
  }

  // 消费实际执行结果、更新步骤和证据
  afterToolCall(context: AfterToolCallContext): void {
    if (context.isError) {
      const verification = context.toolCall.name === ToolName.RUN_TESTS
        ? this.options.getVerificationOutcome?.(context.toolCall.id)
        : undefined;
      if (verification && this.options.planStore && this.#activePlanId()) {
        this.options.planStore.recordVerificationOutcome(this.#activePlanId()!, {
          toolCallId: context.toolCall.id,
          scope: verification.scope,
          testedDigest: verification.testedDigest,
          passed: false,
          failureCode: verification.failureCode,
        });
        this.#log(`plan.verification.failed scope=${verification.scope} callId=${context.toolCall.id} code=${verification.failureCode ?? "test_failed"}`);
      }
      this.#log(`tool.failed name=${context.toolCall.name} callId=${context.toolCall.id} reason=pi_error`);
      return;
    }
    const details = asRuntimeToolDetails(context.result.details);
    if (details?.code !== ToolResultCode.OK) {
      this.#log(`tool.failed name=${context.toolCall.name} callId=${context.toolCall.id} code=${details?.code ?? "missing_details"}`);
      return;
    }
    if (
      this.options.planStore
      && (context.toolCall.name === ToolName.READ_FILE || context.toolCall.name === ToolName.SEARCH_CODE)
    ) {
      this.options.planStore.recordObservationToolCall(context.toolCall.id);
      this.#log(`plan.observation.recorded tool=${context.toolCall.name} callId=${context.toolCall.id}`);
    }
    if (context.toolCall.name === "update_plan_step" || context.toolCall.name === "revise_plan") {
      try { this.#logPlanMutation(context.toolCall.name, context.args); } catch { /* tool itself is the source of the controlled failure */ }
    }
  if (context.toolCall.name === ToolName.READ_FILE) {
      const path = asNonEmptyString(context.args, "path");
      if (path) {
        this.#readSourceFiles.add(path);
        this.#log(`completion.read_evidence count=${this.#readSourceFiles.size} path=${path}`);
      }
    }
    if (context.toolCall.name === ToolName.WRITE_ARTIFACT && details.artifactPath) {
      this.#writtenArtifacts.add(details.artifactPath);
      this.#log(`completion.artifact_evidence path=${details.artifactPath}`);
    }

    const stepId = asNonEmptyString(context.args, "planStepId");
    const activePlanId = this.#activePlanId();
    if (details?.evidenceDraft && stepId && this.options.planStore && activePlanId) {
      const step = this.options.planStore.get(activePlanId).steps.find((item) => item.id === stepId);
      if (!step || !isEvidenceCompatibleWithStep(details.evidenceDraft.kind, step)) {
        this.#log(`plan.evidence.rejected tool=${context.toolCall.name} callId=${context.toolCall.id} stepId=${stepId} evidenceKind=${details.evidenceDraft.kind} reason=STEP_EVIDENCE_KIND_MISMATCH`);
        return;
      }
      const evidence = this.options.planStore.recordToolEvidence(activePlanId, stepId, {
        ...details.evidenceDraft, toolCallId: context.toolCall.id,
      });
      // TEST Evidence 与受测工作区摘要一起写入领域账本，不能由模型自行声称。
      if (details.verification) {
        this.options.planStore.recordVerificationOutcome(activePlanId, {
          toolCallId: context.toolCall.id,
          scope: details.verification.scope,
          testedDigest: details.verification.testedDigest,
          passed: true,
        });
      }
      this.#log(`plan.evidence.recorded id=${evidence.id} stepId=${evidence.stepId} kind=${evidence.kind} codeVersion=${evidence.codeVersion}`);
    }
  }

  // 检查完成、等待、暂停和运行限制
  shouldStopAfterTurn(context: ShouldStopAfterTurnContext): boolean {
    if (this.#stopReason) {
      this.#log(`loop.stop reason=${this.#stopReason} detail=already_decided`);
      return true;
    }
    this.#currentTurn += 1;
    this.#log(`turn.evaluated number=${this.#currentTurn}/${this.options.maxTurns} toolResults=${context.toolResults.length}`);
    if (this.#currentTurn >= this.options.maxTurns) {
      this.#setStopReason(StopReason.MAX_TURNS_REACHED);
      this.#log(`loop.stop reason=${StopReason.MAX_TURNS_REACHED} detail=turn_budget_exhausted`);
      return true;
    }
    // 模型不调用工具只是自然 Final 候选；证据不足时继续经 Pi Follow-up 同一 Run 运行。
    if (context.toolResults.length === 0) {
      const missing = this.getMissingCompletionEvidence();
      if (missing.length === 0) {
        this.#setStopReason(StopReason.COMPLETION_CONTRACT_SATISFIED);
        this.#log(`loop.stop reason=${StopReason.COMPLETION_CONTRACT_SATISFIED} detail=natural_final_and_contract_satisfied`);
        return true;
      }
      this.#pendingFollowUps.push(createFinalRejectedFollowUp(missing));
      this.#log(`loop.continue reason=FINAL_REJECTED missing=${missing.length}`);
    } else {
      this.#log("loop.continue reason=tool_results_present");
    }
    return false;
  }
  takeFollowUpMessages(): AgentMessage[] { return this.#pendingFollowUps.splice(0); }
  getMissingCompletionEvidence(): string[] {
    const missing: string[] = [];
    if (this.#completion.kind === "code_understanding") {
      const remaining = this.#completion.minimumReadFiles - this.#readSourceFiles.size;
      if (remaining > 0) missing.push(`Read ${remaining} more related source file(s); ${this.#readSourceFiles.size}/${this.#completion.minimumReadFiles} successful reads are recorded.`);
    } else {
      const store = this.options.planStore;
      const activePlanId = this.#activePlanId();
      const requiredPlanId = this.#completion.requiredPlanId ?? activePlanId;
      if (!store || !requiredPlanId || (this.#completion.requiredPlanId && activePlanId !== this.#completion.requiredPlanId)) {
        missing.push(activePlanId ? "The active repair plan does not match this run's completion contract." : "Create one server-owned execution plan before declaring completion.");
      } else {
        const plan = store.get(requiredPlanId);
        const replacedStepIds = new Set(plan.revisions.flatMap((revision) => revision.replacedStepIds));
        const unfinished = plan.steps.filter((step) => (
          step.status !== StepStatus.COMPLETED
          && !(step.status === StepStatus.SKIPPED && replacedStepIds.has(step.id))
        ));
        if (unfinished.length > 0) missing.push(`Complete all repair plan steps; unfinished: ${unfinished.map((step) => `${step.id}=${step.status}`).join(", ")}.`);
        // Step status 是历史状态；验证结论还必须对当前受控代码和最新 scope 结果有效。
        missing.push(...store.completionIssues(requiredPlanId));
      }
    }
    if (this.#completion.kind === "code_understanding" && !this.#writtenArtifacts.has(this.#completion.requiredArtifactPath)) {
      missing.push(`Write the required login-flow artifact: ${this.#completion.requiredArtifactPath}.`);
    }
    return missing;
  }
  getState(): RunStopState { return Object.freeze({ runId: this.options.runId, currentTurn: this.#currentTurn, maxTurns: this.options.maxTurns, ...(this.#stopReason ? { stopReason: this.#stopReason } : {}), actionCounts: Object.freeze(Object.fromEntries(this.#actionCounts)), readSourceFiles: Object.freeze([...this.#readSourceFiles].sort()), writtenArtifacts: Object.freeze([...this.#writtenArtifacts].sort()), pendingFollowUps: Object.freeze([...this.#pendingFollowUps]) }); }
  #logPlanMutation(toolName: "update_plan_step" | "revise_plan", args: unknown): void {
    const activePlanId = this.#activePlanId();
    if (!this.options.planStore || !activePlanId) return;
    const plan = this.options.planStore.get(activePlanId);
    const statuses = plan.steps.map((step) => `${step.id}:${step.status}`).join(",");
    const requestedAction = toolName === "update_plan_step" ? asNonEmptyString(args, "action") ?? "unknown" : undefined;
    const stepId = toolName === "update_plan_step" ? asNonEmptyString(args, "stepId") : undefined;
    this.#log(`plan.changed operation=${toolName} ${requestedAction ? `action=${requestedAction} ` : ""}${stepId ? `stepId=${stepId} ` : ""}version=${plan.version} steps=[${statuses}] revisions=${plan.revisions.length}`);
  }
  #setStopReason(reason: StopReason): void { this.#stopReason ??= reason; }
  #log(message: string): void { this.options.onLog?.(`[run:${this.options.runId}] ${message}`); }
}

export function createRunStopHooks(center: RunStopCenter) {
  return {
    beforeToolCall: async (context: BeforeToolCallContext): Promise<BeforeToolCallResult | undefined> => center.beforeToolCall(context),
    afterToolCall: async (context: AfterToolCallContext) => { center.afterToolCall(context); return undefined; },
    shouldStopAfterTurn: async (context: ShouldStopAfterTurnContext): Promise<boolean> => center.shouldStopAfterTurn(context),
    // 在模型提前结束时，提供明确的完成缺项
    getFollowUpMessages: async (): Promise<AgentMessage[]> => center.takeFollowUpMessages(),
  };
}
function normalizeContract(contract: CompletionContract | CompletionRequirements): CompletionContract { return "kind" in contract ? contract : { kind: "code_understanding", ...contract }; }
function requiresPlanStepId(name: string): boolean {
  return name === ToolName.WRITE_ARTIFACT || name === ToolName.MODIFY_CODE || name === ToolName.APPLY_PATCH || name === ToolName.RUN_TESTS;
}

/**
 * Step 的 kind 是服务端计划语义，而不仅是提示词标签：副作用工具必须运行在
 * 能消费其 Evidence 的步骤类型中。否则“诊断阶段跑测试”会留下 test Evidence，
 * “诊断阶段写文档”会伪装为 change Evidence，污染审计链。
 */
function requiredStepKind(toolName: string): StepKind | undefined {
  if (toolName === ToolName.MODIFY_CODE || toolName === ToolName.APPLY_PATCH) return StepKind.IMPLEMENTATION;
  if (toolName === ToolName.RUN_TESTS) return StepKind.VERIFICATION;
  return undefined;
}

function isEvidenceCompatibleWithStep(kind: EvidenceKind, step: PlanStep): boolean {
  if (kind === EvidenceKind.CHANGE) return step.kind === StepKind.IMPLEMENTATION;
  if (kind === EvidenceKind.TEST) return step.kind === StepKind.VERIFICATION;
  return step.kind === StepKind.INVESTIGATION || step.kind === StepKind.DESIGN;
}
export function actionFingerprint(toolName: string, parameters: unknown): string { return `${toolName}:${JSON.stringify(canonicalize(parameters))}`; }
function canonicalize(value: unknown): unknown { if (Array.isArray(value)) return value.map(canonicalize); if (value && typeof value === "object") return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b, "en")).map(([key, nested]) => [key, canonicalize(nested)])); return value; }
function asRuntimeToolDetails(value: unknown): RuntimeToolDetails | undefined { return value && typeof value === "object" && typeof (value as Partial<RuntimeToolDetails>).code === "string" ? value as RuntimeToolDetails : undefined; }
function asNonEmptyString(value: unknown, key: string): string | undefined { const candidate = value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined; return typeof candidate === "string" && candidate.length > 0 ? candidate : undefined; }
function blockedResult(reason: StopReason): BeforeToolCallResult { return { block: true, terminate: true, reason: `[${reason}] This run has already stopped.` }; }
function createFinalRejectedFollowUp(missing: readonly string[]): AgentMessage {
  const repairContinuation = missing.some((item) => item.startsWith("Complete all repair plan steps"))
    ? " For a repair plan: call get_plan if needed; server-ready pending steps must be started with update_plan_step(action=start). Then perform the required governed tool action, record its real evidence, and request action=complete with those evidence IDs. Do not stop after revising a plan."
    : "";
  return {
    role: "user",
    content: `[${StopDecisionCode.FINAL_REJECTED}] Delivery evidence is incomplete:\n${missing.map((item) => `- ${item}`).join("\n")}\nContinue using tools to collect the missing evidence and complete the required artifact.${repairContinuation}`,
    timestamp: Date.now(),
  };
}
