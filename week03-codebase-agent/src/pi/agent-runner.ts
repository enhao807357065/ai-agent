import { randomUUID } from "node:crypto";
import { canonicalJson } from "../store.js";
import { runChecksum, workspaceDigest, type DurableRun } from "../checkpoint/run-checkpoint.js";
import { CodeVersionTracker } from "../planning/code-version.js";
import { EvidenceKind, PlanStore, PlanStoreError, StepKind, StepStatus, type Plan } from "../planning/plan-store.js";
import { PlanToolHandlers } from "../planning/plan-tools.js";
import { RunPlanContext } from "../planning/run-plan-context.js";
import { Type, type Static } from "typebox";
import { runAgentLoop, runAgentLoopContinue, type AgentEvent, type AgentLoopConfig, type AgentMessage, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Message, SystemMessage, ToolResultMessage, UserMessage } from "@earendil-works/pi-ai";
import { costUsdOf, freshTokensOf, LoopGuard } from "../loop-guard.js";
import { renderContext, buildModelInput, assertInputBudget, type ContextStats, type RenderableMessage } from "../context-budget.js";
import type { PlatformConfig } from "../config.js";
import type { Platform } from "../platform.js";
import { executionContext } from "../platform.js";
import { deciderLabel, selectDecider, type DeciderRequest, type ScriptedStep } from "./decider.js";
import { deliverableLine, verifyDeliverable } from "../deliverable.js";
import { renderSystemPrompt } from "./prompt-client.js";

export interface RunAgentInput {
  prompt: string;
  userId: string;
  tenantId: string;
  roles: string[];
  agentName?: string;
  mode?: "plan" | "execute";
  onText?: (delta: string) => void;
  /** 给了脚本就用离线脚本决策器，测试与 CI 因此完全不联网。 */
  script?: ScriptedStep[];
  /** 点名一份离线脚本文件；没点名时用任务条目里配的 script。 */
  scriptPath?: string;
  /**
   * 每走一步回调一次"这一步是谁在主事"。决策器可插拔的代价是看不出来是谁在决策，
   * 这两条回调（外加开头那条 selection）就是补上的可见性。
   */
  onDeciderStep?: (line: string) => void;
  /** 每轮工具使用汇总：按模型提出的 Tool Call 统计，包含被 Runtime 拒绝的调用；它回答“这轮试图调用了什么”。 */
  onTurnTools?: (line: string) => void;
  /** 开头那句"这一趟由谁决策"：口径、降级原因都在这行里，打印与记账共用一份文本。 */
  onDeciderSelected?: (line: string) => void;
  /** 收口那句"交付物有没有账"：两条入口印同一句话，所以文本由 deliverableLine 统一生成。 */
  onDeliverable?: (line: string) => void;
  /** 决策口径：auto（默认，有离线脚本就离线）/ mock（只准离线）/ real（只准真模型，无密钥可降级但会说破）。 */
  decider?: DeciderRequest;
  /** Durable run identity supplied by `agent --resume`; a new run gets a random id. */
  runId?: string;
  /** Resume restores the frozen transcript so the model continues after the approved tool result. */
  resume?: {
    traceId: string;
    context: ReturnType<typeof executionContext>;
    messages: readonly AgentMessage[];
    activePlanId?: string;
    scriptedResponseCursor?: number;
    codeRevision?: number;
    /** Approved frozen action replayed by CLI; runner records its server-owned Plan evidence before the next model turn. */
    executedApproval?: { toolCallId: string; toolName: string; args: Record<string, unknown>; result: { ok: boolean; code: string; content: unknown } };
  };
  /** `--plan` creates/attaches an executable PlanStore DAG; it is distinct from legacy policy `--mode plan`. */
  durablePlan?: boolean;
}

export async function runManagedAgent(platform: Platform, input: RunAgentInput) {
  const runId = input.runId ?? randomUUID();
  const traceId = input.resume?.traceId ?? randomUUID();
  const context = input.resume?.context ?? executionContext({
    traceId,
    requestId: randomUUID(),
    userId: input.userId,
    tenantId: input.tenantId,
    roles: input.roles,
    agentName: input.agentName,
    mode: input.mode,
    workspace: platform.config.sandbox.root,
  });
  const initialWorkspaceDigest = workspaceDigest(platform.config.sandbox.root);
  const planContext = new RunPlanContext();
  const codeRevision = input.resume?.codeRevision ?? 0;
  const codeVersion = new CodeVersionTracker(codeRevision);
  let durablePlan: PlanStore | undefined;
  if (input.resume?.activePlanId) {
    durablePlan = new PlanStore(platform.store, codeVersion);
    planContext.restore(input.resume.activePlanId, durablePlan);
  } else if (input.durablePlan) {
    durablePlan = new PlanStore(platform.store, codeVersion);
  }
  let messages: AgentMessage[] = [...(input.resume?.messages ?? [])];
  const persistRun = (status: DurableRun["status"], pendingApprovalId?: string, result?: unknown): void => {
    const digest = workspaceDigest(platform.config.sandbox.root);
    const run: DurableRun = {
      version: 1, id: runId, traceId, context, prompt: input.prompt, task: platform.config.task?.id ?? null,
      workspace: { root: platform.config.sandbox.root, initialDigest: initialWorkspaceDigest, digest },
      status, messages, scriptedResponseCursor, codeRevision: codeVersion.revision(), planningEnabled: Boolean(durablePlan), plan: durablePlan && planContext.activePlanId() ? durablePlan.get(planContext.activePlanId()!) : undefined,
      activePlanId: planContext.activePlanId(), pendingApprovalId, result,
    };
    platform.store.saveRun({
      id: runId, traceId, status, workspace: platform.config.sandbox.root, workspaceDigest: digest,
      payloadJson: canonicalJson(run), checksum: runChecksum(run), pendingApprovalId,
    });
  };
  const promptBundle = await renderSystemPrompt(platform.config, context);
  platform.store.addTrace({
    traceId,
    eventType: "prompt_rendered",
    payload: {
      prompt_name: platform.config.gateway.promptName,
      source: promptBundle.source,
      version: promptBundle.version,
      content_hash: promptBundle.contentHash,
      task_id: platform.config.task?.id ?? null,
    },
  });
  let scriptedResponseCursor = input.resume?.scriptedResponseCursor ?? 0;
  if (!Number.isInteger(scriptedResponseCursor) || scriptedResponseCursor < 0) throw new Error("SCRIPT_CURSOR_INVALID");
  const { decider, selection } = selectDecider(platform.config, {
    requested: input.decider,
    scriptPath: input.scriptPath,
    inlineScript: input.script,
    scriptedStepOffset: scriptedResponseCursor,
  });
  const { models, model } = decider;
  const selectionLine = `[决策器] ${deciderLabel(selection)} · ${selection.reason}`;
  platform.store.addTrace({
    traceId,
    eventType: "decider.selected",
    payload: { label: deciderLabel(selection), ...selection },
  });
  input.onDeciderSelected?.(selectionLine);  const guard = new LoopGuard(platform.config.runtime);
  /**
   * 每次请求模型之前渲染一次上下文（P3）。这里攒的是每次渲染的统计：
   * 交付时要能说出"第几轮压掉了多少步、剩多少字符"，而不是"我觉得上下文变短了"。
   */
  const contextRenders: ContextStats[] = [];
  const inputBudgetStops: Array<{ input_tokens: number; max_input_tokens: number }> = [];
  // 这份 snapshot 同时给模型声明工具、给 Runtime 按同一 RegisteredTool.parameters 校验。
  const visibleTools = platform.registry.snapshot(context, platform.policy);
  const task = platform.config.task;
  const recordTrustedEvidence = (input: {
    toolCallId: string;
    toolName: string;
    args: Record<string, unknown>;
    output: unknown;
  }): void => {
    const planId = planContext.activePlanId();
    if (!durablePlan || !planId) return;
    const runningStep = durablePlan.get(planId).steps.find((step) => step.status === StepStatus.RUNNING);
    const kind = evidenceKindForTool(input.toolName);
    if (!runningStep || !kind) return;
    if (kind === EvidenceKind.CHANGE) codeVersion.advanceAfterCodeChange();
    const evidence = durablePlan.recordToolEvidence(planId, runningStep.id, {
      toolCallId: input.toolCallId,
      kind,
      summary: `${input.toolName}: ${asText(input.output)}`,
      workspaceDigest: workspaceDigest(platform.config.sandbox.root),
    });
    platform.store.addTrace({
      traceId,
      eventType: "plan.evidence_recorded",
      payload: {
        run_id: runId, plan_id: planId, step_id: runningStep.id, evidence_id: evidence.id,
        evidence_kind: kind, tool_call_id: input.toolCallId, tool_name: input.toolName,
        code_version: evidence.codeVersion, workspace_digest: evidence.workspaceDigest,
      },
    });
    if (kind === EvidenceKind.TEST) {
      const outcome = {
        toolCallId: input.toolCallId,
        scope: typeof input.args.test_file === "string" ? input.args.test_file : "all",
        testedDigest: workspaceDigest(platform.config.sandbox.root),
        passed: testPassed(input.output),
      };
      durablePlan.recordVerificationOutcome(planId, outcome);
      platform.store.addTrace({
        traceId,
        eventType: "plan.verification_recorded",
        payload: { run_id: runId, plan_id: planId, step_id: runningStep.id, tool_call_id: input.toolCallId, ...outcome },
      });
    }
  };
  if (input.resume?.executedApproval?.result.ok) {
    // `agent --resume` executes a frozen call before creating this Runner. Reconstruct
    // its server-owned evidence here, exactly once, before the next model request.
    recordTrustedEvidence({
      toolCallId: input.resume.executedApproval.toolCallId,
      toolName: input.resume.executedApproval.toolName,
      args: input.resume.executedApproval.args,
      output: input.resume.executedApproval.result.content,
    });
  }
  const recordPlanTrace = (eventType: string, payload: Record<string, unknown>): void => {
    platform.store.addTrace({ traceId, eventType, payload: { run_id: runId, ...payload } });
  };
  const recordPlanFailure = (toolCallId: string, action: string, stepId: string | undefined, error: unknown): void => {
    const code = error instanceof PlanStoreError ? error.code : "PLAN_TOOL_ERROR";
    const message = error instanceof Error ? error.message : String(error);
    recordPlanTrace("plan.transition_rejected", {
      tool_call_id: toolCallId, action, step_id: stepId ?? null, code, message,
    });
  };
  const planTools = durablePlan
    ? createPlanTools({
        handlers: new PlanToolHandlers(durablePlan, planContext),
        planId: `plan:${runId}`,
        traceId,
        onTransition: recordPlanTrace,
        onRejected: recordPlanFailure,
      })
    : [];
  const planToolNames = new Set(planTools.map((tool) => tool.name));
  const planRequiredSchema = Type.Object({}, { additionalProperties: true });
  const tools = [
    ...visibleTools.map<AgentTool>((tool) => ({
    name: tool.modelName,
    label: tool.metadata.internalName,
    description: tool.metadata.description,
    parameters: durablePlan && !planContext.hasActivePlan() ? planRequiredSchema : tool.metadata.parameters,
    executionMode: tool.metadata.executionMode,
    async execute(toolCallId, args) {
      const result = await platform.runtime.executePreparedById(
        context,
        toolCallId,
        tool.modelName,
        args as Record<string, unknown>,
      );
      if (!result.ok) throw new Error(`${result.code}: ${asText(result.content)}`);
      recordTrustedEvidence({ toolCallId, toolName: tool.modelName, args: args as Record<string, unknown>, output: result.content });
      return {
        content: [{ type: "text", text: asText(result.content) }],
        details: { code: result.code, attempts: result.attempts, governed: true },
      };
    },
  })),
    ...planTools,
  ];
  const effectiveSystemPrompt = durablePlan
    ? `${promptBundle.prompt}\n\n${DURABLE_PLAN_PROTOCOL}`
    : promptBundle.prompt;
  const systemMessage: SystemMessage = { role: "system", content: effectiveSystemPrompt, timestamp: Date.now() };
  const userMessage: UserMessage = { role: "user", content: input.prompt, timestamp: Date.now() };
  let stop = { stop: false, code: "CONTINUE", reason: "预算允许继续" };
  let blockedByGate = false;
  /**
   * 模型发出的原始参数，按 toolCallId 存一份。上游校验器（pi 那一层）会在我们的门口之前
   * 就把不合格的参数挡掉，这类调用不会走到 prepare()，审计里的 input 就成了空的——
   * 事后翻账看不出"它当时到底发了什么"。这里补上，被拒的调用也留得下原始参数。
   */
  const rawArgsByToolCall = new Map<string, Record<string, unknown>>();
  /** 第几步 = 第几次拿到 assistant 消息；这一位只用来打印身份。 */
  const stepIdentity = { label: deciderLabel(selection), steps: scriptedResponseCursor };

  try {
    const loopConfig: AgentLoopConfig = {
      model,
      convertToLlm: (items) => items.filter(isLlmMessage),
      transformContext: async (messages) => {
        // pi 写明这个钩子不许抛：渲染出岔子就把原文交回去，宁可多烧一点上下文也不能把 Loop 弄断。
        try {
          const rendered = renderContext(messages as unknown as RenderableMessage[], platform.config.context);
          contextRenders.push(rendered.stats);
          platform.store.addTrace({
            traceId,
            eventType: "context.rendered",
            payload: { source: "cli", seq: contextRenders.length, ...rendered.stats },
          });
          return rendered.messages as unknown as typeof messages;
        } catch {
          return messages;
        }
      },
      prepareRequest: async ({ context: requestContext }) => {
        // 请求入口的最后一道预算门：系统提示词、历史、以及每份工具 schema 都已就绪后才计算。
        // 超限时抛出稳定码，Pi 不会创建 provider stream，因此离线与真实模型都不会发请求。
        const modelInput = buildModelInput({
          systemPrompt: promptBundle.prompt,
          history: requestContext.messages as unknown as RenderableMessage[],
          registry: platform.registry,
          context,
          policy: platform.policy,
        });
        const registeredToolTokens = schemaTokens(modelInput.tools.map((tool) => ({
          name: tool.modelName,
          description: tool.metadata.description,
          parameters: tool.metadata.parameters,
        })));
        const inputTokens = modelInput.inputTokens - registeredToolTokens + schemaTokens(tools);
        try {
          assertInputBudget({ inputTokens, maxInputTokens: platform.config.context.maxInputTokens });
        } catch (error) {
          inputBudgetStops.push({ input_tokens: inputTokens, max_input_tokens: platform.config.context.maxInputTokens });
          platform.store.addTrace({
            traceId,
            eventType: "input_budget.exceeded",
            payload: { input_tokens: inputTokens, max_input_tokens: platform.config.context.maxInputTokens },
          });
          throw error;
        }
        platform.store.addTrace({
          traceId,
          eventType: "input.prepared",
          payload: { input_tokens: inputTokens, tool_count: tools.length },
        });
      },
      reasoning: undefined,
      maxTokens: platform.config.model.maxTokens,
      beforeToolCall: async ({ toolCall, args }) => {
        const guardDecision = guard.beforeToolCall(toolCall.name, args as Record<string, unknown>);
        if (!guardDecision.allow) {
          if (!planToolNames.has(toolCall.name)) {
            platform.runtime.recordPiImmediateFailure({
              context,
              toolCallId: toolCall.id,
              modelName: toolCall.name,
              args: args as Record<string, unknown>,
              code: guardDecision.code,
              message: guardDecision.reason,
            });
          }
          return { block: true, reason: `${guardDecision.code}: ${guardDecision.reason}` };
        }
        if (planToolNames.has(toolCall.name)) return undefined;
        if (durablePlan && !planContext.hasActivePlan()) {
          const reason = "--plan 模式必须先调用 create_plan 创建活动计划。";
          platform.runtime.recordPiImmediateFailure({
            context,
            toolCallId: toolCall.id,
            modelName: toolCall.name,
            args: args as Record<string, unknown>,
            code: "PLAN_REQUIRED",
            message: reason,
          });
          return { block: true, reason: `PLAN_REQUIRED: ${reason}` };
        }
        if (durablePlan) {
          const planId = planContext.activePlanId()!;
          const running = durablePlan.get(planId).steps.filter((step) => step.status === StepStatus.RUNNING);
          if (running.length !== 1) {
            const reason = "--plan 模式必须先 start 一个且仅一个运行中的计划步骤，才能执行仓库工具。";
            platform.runtime.recordPiImmediateFailure({
              context,
              toolCallId: toolCall.id,
              modelName: toolCall.name,
              args: args as Record<string, unknown>,
              code: "PLAN_STEP_REQUIRED",
              message: reason,
            });
            return { block: true, reason: `PLAN_STEP_REQUIRED: ${reason}` };
          }
          const expectedEvidence = evidenceKindForTool(toolCall.name);
          if (expectedEvidence && !stepAllowsEvidence(running[0].kind, expectedEvidence)) {
            const reason = `运行中的步骤 ${running[0].id}（${running[0].kind}）不允许 ${toolCall.name} 产生 ${expectedEvidence} 证据；请先完成该步骤并 start 与动作类型匹配的下一步骤。`;
            platform.runtime.recordPiImmediateFailure({
              context,
              toolCallId: toolCall.id,
              modelName: toolCall.name,
              args: args as Record<string, unknown>,
              code: "PLAN_STEP_KIND_REQUIRED",
              message: reason,
            });
            recordPlanTrace("plan.tool_rejected", {
              tool_call_id: toolCall.id, plan_id: planId, step_id: running[0].id,
              step_kind: running[0].kind, tool_name: toolCall.name, evidence_kind: expectedEvidence,
              code: "PLAN_STEP_KIND_REQUIRED", reason,
            });
            return { block: true, reason: `PLAN_STEP_KIND_REQUIRED: ${reason}` };
          }
        }
        const decision = await platform.runtime.gateForPi({
          context,
          toolCallId: toolCall.id,
          modelName: toolCall.name,
          args: args as Record<string, unknown>,
        });
        if (!decision.block) return undefined;
        // 人工拒了、图连不上、没人有资格批——这三条路都给 terminate：这一批工具结果之后 Loop 就收口，
        // 最终回答停在"被拦死"这个事实上。让模型接着讲一个和结局相反的收尾，是 Harness 不该纵容的事。
        if (decision.terminate) {
          blockedByGate = true;
          stop = { stop: true, code: decision.code ?? "GATE_BLOCKED", reason: decision.message ?? "Runtime 拦下了这次调用" };
          return { block: true, reason: decision.reason, terminate: true };
        }
        return { block: true, reason: decision.reason };
      },
      afterToolCall: async ({ result, isError }) => ({
        details: { ...(asRecord(result.details)), governed: true, trace_id: traceId },
        isError,
      }),
      finishTurn: async (turn) => {
        // 门口已经被拦死过一次，这一轮就没有"继续"可言：结局由拦死的那条决定说话。
        if (blockedByGate) return { action: "end" as const };
        const budgetStop = guard.shouldStop(platform.runtime.pendingApproval(traceId));
        if (budgetStop.stop) {
          stop = budgetStop;
          return { action: "end" as const };
        }
        // 模型自然结束只是“没有继续提工具”，不是“交付已经完成”。
        // 当前最小收口不伪造额外模型回合：缺项会在最终验收结果和 Trace 中明确报告。
        const askedForWork = turn.message.content.some((part) => part.type === "toolCall");
        stop = askedForWork
          ? { stop: false, code: "CONTINUE", reason: "预算允许继续" }
          : { stop: true, code: "COMPLETED", reason: "模型给出最终回答，等待服务端交付验收" };
        return { action: askedForWork ? "continue" : "end" };
      },
    };
    const newlyProduced = messages.length > 0
      ? await runAgentLoopContinue(
          { messages, tools },
          loopConfig,
          async (event) => observeEvent(platform, context, guard, event, rawArgsByToolCall, input, stepIdentity),
          undefined,
          models.streamSimple.bind(models),
        )
      : await runAgentLoop(
          [systemMessage, userMessage],
          { messages: [], tools },
          loopConfig,
          async (event) => observeEvent(platform, context, guard, event, rawArgsByToolCall, input, stepIdentity),
          undefined,
          models.streamSimple.bind(models),
        );
    messages = [...messages, ...newlyProduced];
    // Persist cursor from actual newly received scripted assistant responses, never from
    // aggregate Pi history (which also includes replayed ToolResult/system messages).
    if (selection.kind === "scripted") {
      scriptedResponseCursor += newlyProduced.filter((message) => message.role === "assistant").length;
    }
  } catch (error) {
    if (inputBudgetStops.length === 0) throw error;
  }

  const lastAssistant = [...messages].reverse().find((message): message is AssistantMessage => message.role === "assistant");
  const finalAnswer = lastAssistant?.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("") ?? "";
  const verification = verifyDeliverable(platform.store, {
    traceId,
    // 名单取装配之后的那份（platform.config.hitl），不是 tasks.yaml 原文：命令行覆盖掉的东西不该被写进账。
    task: task ? { id: task.id, label: task.displayName, deliverable: task.deliverable, approvers: platform.config.hitl.approvers, testCommand: task.testCommand } : undefined,
    decider: { kind: selection.kind, label: deciderLabel(selection), script: selection.script, degraded: selection.degraded },
    finalAnswer,
  });
  const finalPlan = durablePlan && planContext.activePlanId()
    ? durablePlan.get(planContext.activePlanId()!)
    : undefined;
  const planIssues = finalPlan
    ? planAcceptanceIssues(finalPlan, durablePlan!.completionIssues(finalPlan.id))
    : [];
  if (planIssues.length > 0) {
    verification.missing.push(...planIssues);
    platform.store.addTrace({ traceId, eventType: "plan.verified", payload: { plan_id: planContext.activePlanId(), issues: planIssues } });
  }
  const effectiveStop = inputBudgetStops.length > 0
    ? {
        stop: true,
        code: "INPUT_BUDGET_EXCEEDED",
        reason: `本轮输入估算 ${inputBudgetStops.at(-1)!.input_tokens} tokens，超过上限 ${inputBudgetStops.at(-1)!.max_input_tokens} tokens`,
      }
    : stop;
  const execution = {
    status: effectiveStop.code === "COMPLETED" ? "ended" : effectiveStop.code.startsWith("MAX_") || effectiveStop.code === "INPUT_BUDGET_EXCEEDED" ? "budget_exhausted" : effectiveStop.code === "AWAITING_APPROVAL" ? "waiting_approval" : "stopped",
    stop: effectiveStop,
  };
  const acceptance = {
    status: verification.missing.length === 0 ? "passed" : "failed",
    missing: verification.missing,
    tests: verification.tests,
  };
  if (acceptance.status === "failed" && execution.status === "ended") {
    effectiveStop.stop = true;
    effectiveStop.code = "INCOMPLETE_EVIDENCE";
    effectiveStop.reason = verification.missing.join("；");
  }
  // 两条入口留同一类事件：事后翻账时，CLI 跑的那趟和 pi 跑的那趟要能放在一起比。
  platform.store.addTrace({
    traceId,
    eventType: "deliverable.verified",
    payload: { ...verification, execution, acceptance } as unknown as Record<string, unknown>,
  });
  input.onDeliverable?.(deliverableLine(verification));
  const result = {
    run_id: runId,
    trace_id: traceId,
    task: task?.id ?? null,
    decider: {
      kind: selection.kind,
      requested: selection.requested,
      label: deciderLabel(selection),
      script: selection.script,
      model_id: selection.modelId,
      reason: selection.reason,
      degraded: selection.degraded,
      steps: stepIdentity.steps,
    },
    /** 交付物回读：模型说没说是一回事，审计里有没有那次成功的写入是另一回事。 */
    deliverable: verification,
    prompt_source: promptBundle.source,
    prompt_version: promptBundle.version,
    prompt_hash: promptBundle.contentHash,
    final_answer: finalAnswer,
    execution,
    acceptance,
    stop: effectiveStop,
    pending_approval_id: platform.runtime.pendingApproval(traceId),
    budget: guard.snapshot(),
    context: {
      renders: contextRenders.length,
      rules: platform.config.context,
      last: contextRenders[contextRenders.length - 1] ?? null,
    },
    message_count: messages.length,
    plan: finalPlan ? planResultSummary(finalPlan, planIssues) : null,
  };
  const pendingApprovalId = platform.runtime.pendingApproval(traceId);
  const status: DurableRun["status"] = pendingApprovalId
    ? "waiting_approval"
    : result.execution.status === "ended" ? "completed"
      : result.execution.status === "budget_exhausted" ? "paused" : "failed";
  persistRun(status, pendingApprovalId, result);
  return result;
}

/** 观察并记账：pi 的每个事件都落一条 Trace，模型自述不进这里。 */
async function observeEvent(
  platform: Platform,
  context: ReturnType<typeof executionContext>,
  guard: LoopGuard,
  event: AgentEvent,
  rawArgsByToolCall: Map<string, Record<string, unknown>>,
  input: RunAgentInput,
  stepIdentity: { label: string; steps: number },
): Promise<void> {
  if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
    input.onText?.(event.assistantMessageEvent.delta);
  }
  if (event.type === "message_end" && event.message.role === "assistant") {
    // 每一步都报一次"这一步是谁在主事"：mock 与 real 换着用之后，这是唯一能当场看出来的地方。
    stepIdentity.steps += 1;
    platform.store.addTrace({
      traceId: context.traceId,
      eventType: "decider.step",
      payload: { step: stepIdentity.steps, label: stepIdentity.label },
    });
    input.onDeciderStep?.(`[决策器 · 第${stepIdentity.steps}步] ${stepIdentity.label}`);
    for (const part of event.message.content) {
      if (part.type === "toolCall") {
        rawArgsByToolCall.set(part.id, part.arguments as Record<string, unknown>);
      }
    }
    guard.observeAssistantUsage({
      totalTokens: freshTokensOf(event.message.usage),
      costUsd: costUsdOf(event.message.usage, platform.config.model),
    });
  }
  if (event.type === "turn_end") {
    guard.observeTurn();
    // turn_end 的 message 是 Pi 的宽泛 AgentMessage 联合；真实 Tool Call 只属于 assistant content。
    const assistant = event.message.role === "assistant" ? event.message : undefined;
    const names = assistant?.content
      .filter((part) => part.type === "toolCall")
      .map((part) => part.name) ?? [];
    const counts = new Map<string, number>();
    for (const name of names) counts.set(name, (counts.get(name) ?? 0) + 1);
    const breakdown = [...counts.entries()].map(([name, count]) => `${name} × ${count}`).join("，") || "无";
    const line = `[本轮工具 · 第${stepIdentity.steps}轮] 共 ${names.length} 次 · ${breakdown}`;
    platform.store.addTrace({
      traceId: context.traceId,
      eventType: "turn.tools",
      payload: { turn: stepIdentity.steps, total: names.length, tools: Object.fromEntries(counts) },
    });
    input.onTurnTools?.(line);
  }
  if (event.type === "tool_execution_end" && event.isError && !platform.runtime.wasAudited(event.toolCallId)) {
    platform.runtime.recordPiImmediateFailure({
      context,
      toolCallId: event.toolCallId,
      modelName: event.toolName,
      args: rawArgsByToolCall.get(event.toolCallId) ?? {},
      message: event.result.content
        .map((part: { type: string; text?: string }) => (part.type === "text" ? part.text ?? "" : "[image]"))
        .join("\n"),
    });
  }
  platform.store.addTrace({
    traceId: context.traceId,
    eventType: `pi.${event.type}`,
    payload: summarizeEvent(event),
  });
}

function testPassed(output: unknown): boolean {
  return /exit_code=0(?:\s|$)/.test(asText(output));
}

const DURABLE_PLAN_PROTOCOL = `## --plan 执行协议（强制）

你正在执行一个持久化 Plan DAG。交付物写成功不等于计划完成，不能宣称“这不影响交付物”。

1. 先调用 create_plan。每个 step 必须声明正确 kind 和 depends_on：
   - investigation / design：只读调查；完成必须有服务端 NOTE evidence。
   - implementation：写入或补丁；完成必须有服务端 CHANGE evidence。
   - verification：run_test；完成必须有服务端通过的 TEST evidence。
2. 每次调用仓库工具前，必须先 update_plan_step(action=start) 启动该工具所属步骤。一次只能有一个 RUNNING step。
3. 成功工具调用后，服务端自动记录 Evidence。再调用 update_plan_step(action=complete) 完成当前步骤；不要试图提供或伪造 evidence。
4. 严格按 DAG：必须先 complete 当前 step，后续依赖 step 才能 start。不要并行 start 或 complete 多个尚未启动的步骤。
5. 写入文档属于 implementation step。计划有 step2/step3 时，必须逐一 start → 执行匹配工具 → complete；任何 pending/running step 都会使服务端验收失败。
6. 遇到 PLAN_* 或 STEP_* 错误，先读取错误并修正状态机调用；不要声明任务已完成。`;

function stepAllowsEvidence(stepKind: StepKind, evidenceKind: EvidenceKind): boolean {
  // Investigation/design may perform writes only when the task's final artifact is
  // itself the research report. Completion still requires NOTE evidence; a CHANGE is
  // additionally recorded for the delivery. Verification owns TEST evidence.
  if (stepKind === StepKind.INVESTIGATION || stepKind === StepKind.DESIGN) {
    return evidenceKind === EvidenceKind.NOTE || evidenceKind === EvidenceKind.CHANGE;
  }
  if (stepKind === StepKind.IMPLEMENTATION) return evidenceKind === EvidenceKind.CHANGE;
  return evidenceKind === EvidenceKind.TEST;
}

function evidenceKindForTool(toolName: string): EvidenceKind | undefined {
  if (["read_file", "read_directory", "list_files", "search_code", "git_diff", "git_status"].includes(toolName)) {
    return EvidenceKind.NOTE;
  }
  if (["write_file", "apply_patch"].includes(toolName)) return EvidenceKind.CHANGE;
  if (toolName === "run_test") return EvidenceKind.TEST;
  return undefined;
}

function createPlanTools(input: {
  handlers: PlanToolHandlers;
  planId: string;
  traceId: string;
  onTransition: (eventType: string, payload: Record<string, unknown>) => void;
  onRejected: (toolCallId: string, action: string, stepId: string | undefined, error: unknown) => void;
}): AgentTool[] {
  const createPlanSchema = Type.Object({
    goal: Type.String({ minLength: 1 }),
    constraints: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
    steps: Type.Array(planStepSchema, { minItems: 1 }),
  }, { additionalProperties: false });
  const getPlanSchema = Type.Object({}, { additionalProperties: false });
  const updatePlanStepSchema = Type.Object({
    action: Type.Union([
      Type.Literal("start"),
      Type.Literal("complete"),
    ]),
    step_id: Type.String({ minLength: 1 }),
    evidence_ids: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  }, { additionalProperties: false });
  const revisePlanSchema = Type.Object({
    expected_version: Type.Number({ minimum: 1 }),
    reason: Type.String({ minLength: 1 }),
    evidence_ids: Type.Array(Type.String({ minLength: 1 })),
    replacements: Type.Array(Type.Object({
      replace_step_id: Type.String({ minLength: 1 }),
      step: planStepSchema,
    }, { additionalProperties: false })),
    additions: Type.Optional(Type.Array(planStepSchema)),
  }, { additionalProperties: false });

  return [
    {
      name: "create_plan",
      label: "create_plan",
      description: "创建并激活本次 --plan 运行的计划。创建成功后才可以调用仓库工具。",
      parameters: createPlanSchema,
      executionMode: "sequential",
      async execute(toolCallId, args: unknown) {
        const inputArgs = args as Static<typeof createPlanSchema>;
        try {
          const plan = input.handlers.createPlan(input.planId, inputArgs.steps.map(toPlanStep), {
            goal: inputArgs.goal,
            constraints: inputArgs.constraints,
          });
          input.onTransition("plan.created", {
            tool_call_id: toolCallId, plan_id: plan.id, plan_version: plan.version,
            goal: plan.goal, step_ids: plan.steps.map((step) => step.id),
          });
          return planResult(input.traceId, plan);
        } catch (error) {
          input.onRejected(toolCallId, "create", undefined, error);
          throw error;
        }
      },
    },
    {
      name: "get_plan",
      label: "get_plan",
      description: "读取当前活动计划。",
      parameters: getPlanSchema,
      executionMode: "sequential",
      async execute(toolCallId, _args: unknown) {
        try {
          const plan = input.handlers.getPlan();
          input.onTransition("plan.read", { tool_call_id: toolCallId, plan_id: plan.id, plan_version: plan.version });
          return planResult(input.traceId, plan);
        } catch (error) {
          input.onRejected(toolCallId, "get", undefined, error);
          throw error;
        }
      },
    },
    {
      name: "update_plan_step",
      label: "update_plan_step",
      description: "更新当前计划步骤。action 必须是 start 或 complete。",
      parameters: updatePlanStepSchema,
      executionMode: "sequential",
      async execute(toolCallId, args: unknown) {
        const inputArgs = args as Static<typeof updatePlanStepSchema>;
        try {
          const plan = inputArgs.action === "start"
            ? input.handlers.startStep(inputArgs.step_id)
            : input.handlers.completeStep(inputArgs.step_id, inputArgs.evidence_ids ?? []);
          input.onTransition(inputArgs.action === "start" ? "plan.step_started" : "plan.step_completed", {
            tool_call_id: toolCallId, plan_id: plan.id, plan_version: plan.version,
            step_id: inputArgs.step_id, action: inputArgs.action,
            evidence_ids: inputArgs.action === "complete"
              ? plan.steps.find((step) => step.id === inputArgs.step_id)?.completionEvidenceIds ?? []
              : [],
          });
          return planResult(input.traceId, plan);
        } catch (error) {
          input.onRejected(toolCallId, inputArgs.action, inputArgs.step_id, error);
          throw error;
        }
      },
    },
    {
      name: "revise_plan",
      label: "revise_plan",
      description: "以乐观并发版本号修订当前活动计划。",
      parameters: revisePlanSchema,
      executionMode: "sequential",
      async execute(toolCallId, args: unknown) {
        const inputArgs = args as Static<typeof revisePlanSchema>;
        try {
          const plan = input.handlers.revisePlan({
            expectedVersion: inputArgs.expected_version,
            reason: inputArgs.reason,
            evidenceIds: inputArgs.evidence_ids,
            replacements: inputArgs.replacements.map((replacement) => ({
              replaceStepId: replacement.replace_step_id,
              step: toPlanStep(replacement.step),
            })),
            additions: inputArgs.additions?.map(toPlanStep),
          });
          input.onTransition("plan.revised", {
            tool_call_id: toolCallId, plan_id: plan.id, plan_version: plan.version,
            reason: inputArgs.reason, evidence_ids: inputArgs.evidence_ids,
            replacement_count: inputArgs.replacements.length, addition_count: inputArgs.additions?.length ?? 0,
          });
          return planResult(input.traceId, plan);
        } catch (error) {
          input.onRejected(toolCallId, "revise", undefined, error);
          throw error;
        }
      },
    },
  ];
}

const planStepSchema = Type.Object({
  id: Type.String({ minLength: 1 }),
  title: Type.String({ minLength: 1 }),
  success_criteria: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  kind: Type.Optional(Type.Union([
    Type.Literal("investigation"),
    Type.Literal("design"),
    Type.Literal("implementation"),
    Type.Literal("verification"),
  ])),
  depends_on: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
}, { additionalProperties: false });

function toPlanStep(step: {
  id: string;
  title: string;
  success_criteria?: string[];
  kind?: "investigation" | "design" | "implementation" | "verification";
  depends_on?: string[];
}) {
  return {
    id: step.id,
    title: step.title,
    successCriteria: step.success_criteria,
    kind: step.kind,
    dependsOn: step.depends_on,
  };
}

function planResult(traceId: string, plan: Plan) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(plan) }],
    details: { governed: true, trace_id: traceId, plan_id: plan.id, version: plan.version },
  };
}

function schemaTokens(tools: readonly { name: string; description: string; parameters: unknown }[]): number {
  return Math.ceil(JSON.stringify(tools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
  }))).length / 4);
}

function summarizeEvent(event: AgentEvent): Record<string, unknown> {
  if (event.type === "tool_execution_start" || event.type === "tool_execution_end") {
    return { tool_call_id: event.toolCallId, tool_name: event.toolName, is_error: "isError" in event ? event.isError : false };
  }
  if (event.type === "turn_end") return { tool_result_count: event.toolResults.length };
  if (event.type === "message_end") return { role: event.message.role };
  return { type: event.type };
}

function isLlmMessage(message: AgentMessage): message is Message {
  return message.role === "system" || message.role === "user" || message.role === "assistant" || message.role === "toolResult";
}

function planResultSummary(plan: Plan, issues: readonly string[]) {
  const status = plan.steps.every((step) => step.status === StepStatus.COMPLETED)
    && issues.length === 0
    ? "completed"
    : "incomplete";
  return {
    id: plan.id,
    goal: plan.goal,
    version: plan.version,
    status,
    issues: [...issues],
    steps: plan.steps.map((step) => ({
      id: step.id,
      title: step.title,
      kind: step.kind,
      status: step.status,
      depends_on: step.dependsOn,
      success_criteria: step.successCriteria,
      completion_evidence_ids: step.completionEvidenceIds,
      failure_reason: step.failureReason ?? null,
      blocked_reason: step.blockedReason ?? null,
    })),
    evidence: plan.evidence.map((item) => ({
      id: item.id,
      step_id: item.stepId,
      kind: item.kind,
      tool_call_id: item.toolCallId ?? null,
      code_version: item.codeVersion,
      workspace_digest: item.workspaceDigest ?? null,
      summary: item.summary,
    })),
    verification: plan.verificationOutcomes.map((outcome) => ({
      tool_call_id: outcome.toolCallId,
      scope: outcome.scope,
      tested_digest: outcome.testedDigest,
      passed: outcome.passed,
      failure_code: outcome.failureCode ?? null,
    })),
    revisions: plan.revisions,
  };
}

function planAcceptanceIssues(plan: Plan, completionIssues: readonly string[]): string[] {
  const incomplete = plan.steps.filter((step) => step.status !== StepStatus.COMPLETED);
  return [
    ...completionIssues,
    ...incomplete.map((step) => `计划步骤 ${step.id} 未完成（${step.status}）`),
  ];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
