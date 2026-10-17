import {
  runAgentLoop,
  runAgentLoopContinue,
  type AgentContext,
  type AgentEvent,
  type AgentLoopConfig,
  type AgentMessage,
  type StreamFn,
} from "@earendil-works/pi-agent-core";
import type { Message, Model } from "@earendil-works/pi-ai";
import { SourceWriteApprovalHook } from "../approval/source-write-approval-hook.js";
import { PlanStore } from "../planning/plan-store.js";
import {
  createRunExecutionContext,
  type RunExecutionContext,
} from "../runtime/execution-context.js";
import { ToolRuntime } from "../runtime/tool-runtime.js";
import { createPiRuntimeTools } from "../tools/pi-runtime-tools.js";
import {
  createRunStopHooks,
  RunStopCenter,
  StopReason,
  type CompletionContract,
  type CompletionRequirements,
  type RunStopState,
} from "./run-stop-center.js";

/** 教学默认值；调用方可通过 options.maxTurns 覆盖，而停止判定仍由 RunStopCenter 统一执行。 */
const defaultMaxTurns = 8;

/**
 * 默认完成契约。实际产物路径会在创建 Run 时替换为 executionContext.taskArtifactPath，
 * 以保证“Prompt 要求的产物”和 Runtime 允许写入的产物路径来自同一可信上下文。
 */
const defaultCompletion: CompletionRequirements = {
  minimumReadFiles: 3,
  requiredArtifactPath: "login-flow.md",
};

/**
 * 与具体仓库、业务域无关的 Agent 行为约束。
 *
 * 任务目标只应来自 userTask / CLI 参数；System Prompt 只定义“如何在受控环境中工作”。
 * 权限、路径、DAG 与 Evidence 的最终裁决仍在服务端，Prompt 不承担安全职责。
 */
export const codebaseAgentSystemPrompt = [
  "你是受服务端 PlanStore 治理的 Codebase Agent。任务目标只来自用户任务和当前 [PLAN_STATE]，不得把示例业务当作固定目标。",
  "先根据当前任务和仓库事实判断复杂度：未知根因、跨文件变更、风险操作或多阶段验证时，先诊断再规划；不要把假设当成结论。",
  "计划步骤必须描述可验收结果、dependsOn 和完成标准；Plan 需要记录本次 goal 与 constraints。初始诊断完成后，根据真实 Evidence 用 revise_plan 原子追加、替换或收缩后续 DAG；不要为了迎合旧计划制造无意义修改。",
  "每轮只执行服务端 ready 且未完成的步骤。步骤开始用 update_plan_step(action=start)；受计划治理的副作用工具必须携带对应 planStepId；完成必须引用属于该步骤的真实 evidenceIds。",
  "工具与步骤类型必须匹配：investigation/design 优先读取、搜索与 note Evidence；note Evidence 应携带 sourceToolCallIds 引用同一 Run 成功的 read_file/search_code 调用；modify_code 或 apply_patch 只用于 running implementation；run_tests 只用于 running verification。apply_patch 的每个目标必须先读取；它仅支持不同已有文本文件上的唯一 oldText→newText 替换，任一 hunk 不匹配时不得假称有修改。工具成功不等于步骤完成。",
  "每次工具返回后判断其是否支持当前假设：局部参数错误先局部修正；根因、范围、依赖、风险或验证结果变化时，引用 Evidence 调用 revise_plan。遇到权限、预算或外部依赖阻塞时，记录阻塞，不绕过限制。",
  "不得伪造工具结果、Evidence、代码修改或测试通过；不得删除断言、降低标准或用无关变更制造 change Evidence。若现有实现已满足任务，动态追加真实验证路径而不是修改代码。",
  "自然语言 Final 不代表完成：只有服务端 Completion Contract 满足时才结束。最终报告应区分真实修改、实际验证命令与结果、动态计划调整、以及未验证项。",
  "只输出必要判断和行动依据，不输出完整思维链。",
].join("\n");

/** 作为 runAgentLoop 初始 prompt 进入 Context 的固定用户任务。 */
export const codebaseAgentTask =
  // "分析目标仓库的登录逻辑，并生成 Markdown 登录流程说明文档。";
  "排查并修复 login_demo 的 access token 会话过期边界问题。" +
  "先确认 token 签发、解析和鉴权链路；" +
  "定义 now === expiresAt 时必须失效的边界；" +
  "完成实现与边界验证，并报告未验证项。";

/**
 * 由装配层记录的“模型侧”结束原因。
 * 业务完成、重复动作、最大轮数等原因仍以 RunStopCenter 的 first-write-wins 决策为准。
 */
export const AssemblyStopReason = {
  CANCELLED: "cancelled",
  MODEL_ERROR: "model_error",
} as const;

export type AssemblyStopReason =
  (typeof AssemblyStopReason)[keyof typeof AssemblyStopReason];

export interface AgentTraceEvent {
  readonly type: "turn_started" | "tool_started" | "tool_finished";
  readonly turn: number;
  readonly toolName?: string;
  readonly toolCallId?: string;
  readonly isError?: boolean;
}

/**
 * 面向调用方的 Run 运行时快照。
 * stopCenter 保留证据、动作计数等可审计细节；顶层 stopReason 合并 StopCenter 和模型侧结束原因。
 */
export interface CodebaseAgentRunState {
  readonly runId: string;
  readonly turnCount: number;
  readonly stopReason?: StopReason | AssemblyStopReason;
  readonly stopCenter: RunStopState;
}

/**
 * 装配层的依赖注入入口。
 * model / streamFn 必填，让调用方决定真实 Gateway 或脚本化测试流；其余依赖可注入，
 * 便于隔离仓库、产物目录、取消信号与 Runtime 行为，但不改变唯一 Runtime 执行路径。
 */
export interface RunCodebaseAgentOptions {
  readonly runId?: string;
  readonly model: Model<any>;
  readonly streamFn: StreamFn;
  readonly maxTurns?: number;
  readonly signal?: AbortSignal;
  readonly executionContext?: RunExecutionContext;
  readonly runtime?: ToolRuntime;
  readonly completion?: CompletionContract | CompletionRequirements;
  /** 可选任务覆写：planned repair mode 不继承旧的“写登录流程文档”教学目标。 */
  readonly systemPrompt?: string;
  readonly userTask?: string;
  /** 应用 checkpoint 恢复的既有 Pi 协议消息。只允许 CLI 在完成 fail-closed 检查后传入。 */
  readonly resumeMessages?: readonly AgentMessage[];
  readonly onLog?: (message: string) => void;
  /** 可选 Planner 注入；提供后，写工具必须绑定正在运行的 planStepId。 */
  readonly planStore?: PlanStore;
  readonly planId?: string;
  /** 可选人工审批 Hook：仅拦截 modify_code/apply_patch，批准仍只能消费被冻结的同一调用。 */
  readonly sourceWriteApproval?: SourceWriteApprovalHook;
  readonly onTextDelta?: (text: string) => void;
}

/**
 * Pi Loop 已完成后的可验收输出：
 * - state：停止原因与 Stop Center 的事实快照；
 * - messages：Pi 本次 runAgentLoop 新产生的消息；
 * - trace：由 AgentEvent 归纳的可观测轨迹；
 * - runtime：实际使用的唯一 Runtime，供测试检查 handler 执行次数。
 */
export interface CodebaseAgentRunResult {
  readonly state: CodebaseAgentRunState;
  readonly messages: readonly AgentMessage[];
  readonly trace: readonly AgentTraceEvent[];
  readonly runtime: ToolRuntime;
}

/**
 * 唯一装配点：组装现有上下文、Runtime、Pi 工具、停止保护和 Pi 原生 runAgentLoop。
 * 本模块不实现第二套模型循环、工具执行路径或停止规则。
 */
export async function runCodebaseAgent(
  options: RunCodebaseAgentOptions,
): Promise<CodebaseAgentRunResult> {
  // 这些对象都属于同一个 Run：外部注入优先，默认值只负责教学场景的可运行性。
  const runId = options.runId ?? crypto.randomUUID();
  const executionContext = options.executionContext ?? createRunExecutionContext();
  const runtime = options.runtime ?? new ToolRuntime();
  const planStore = options.planStore;
  const planId = options.planId;
  const log = (message: string): void => options.onLog?.(`[run:${runId}] ${message}`);
  if ((planStore === undefined) !== (planId === undefined)) {
    throw new Error("planStore and planId must be provided together.");
  }

  // Stop Center 不参与执行；它只在 Pi Hooks 时根据本 Run 已知事实作决定。
  const stopCenter = new RunStopCenter({
    runId,
    maxTurns: options.maxTurns ?? defaultMaxTurns,
    completion: options.completion ?? {
      ...defaultCompletion,
      requiredArtifactPath: executionContext.taskArtifactPath,
    },
    onLog: options.onLog,
    ...(planStore === undefined ? {} : { planStore, planId }),
  });
  // taskArtifactPath 是执行上下文的可信配置，优先于硬编码的教学默认文件名。
  const tools = createPiRuntimeTools(runtime, executionContext);
  const trace: AgentTraceEvent[] = [];
  let turnCount = 0;
  let assemblyStopReason: AssemblyStopReason | undefined;

  // AgentContext 决定“模型可看到什么”：Prompt 与 Adapter 产出的工具，不包含 Runtime 内部对象。
  const planRule = planStore === undefined
    ? ""
    : "\n当前 Run 受 PlanStore 治理：写入类工具必须携带正在运行步骤的 planStepId；工具成功不等于步骤 completed，必须等待服务端证据校验。";
  const initialPlanMessages = planStore === undefined ? [] : [createPlanSnapshotMessage(planStore, planId!)];
  const restoredMessages = options.resumeMessages ? options.resumeMessages.filter(isModelMessage) : [];
  log(`run.started maxTurns=${options.maxTurns ?? defaultMaxTurns} planner=${planStore === undefined ? "disabled" : `enabled planId=${planId}`} resumedMessages=${restoredMessages.length}`);
  if (planStore) log(`plan.snapshot.initial ${describePlan(planStore, planId!)}`);
  const context: AgentContext = {
    systemPrompt: `${options.systemPrompt ?? codebaseAgentSystemPrompt}${planRule}`,
    messages: [...restoredMessages.filter((message) => !isPlanSnapshotMessage(message)), ...initialPlanMessages],
    tools,
  };

  // 复用 Stop Center 的 Hook；装配层只接线，不在这里复制重复动作、证据或 Final 规则。
  const stopHooks = createRunStopHooks(stopCenter);
  // 审批先于 Stop Center 的工具执行允许判定；它只返回 Pi 的 block 结果，不旁路 Runtime。
  const approvalHook = options.sourceWriteApproval;
  const originalBeforeToolCall = stopHooks.beforeToolCall;
  const governedBeforeToolCall = async (hookContext: Parameters<NonNullable<typeof originalBeforeToolCall>>[0]) => {
    const approval = approvalHook ? await approvalHook.beforeToolCall(hookContext) : undefined;
    if (approval?.block && approval.reason?.startsWith("[APPROVAL_REQUIRED]")) stopCenter.waitForApproval();
    return approval ?? originalBeforeToolCall?.(hookContext);
  };
  // 回答的问题: Pi 应该怎样运行这个工作台？ 用哪个模型、工具串行还是并行、消息如何转换、生命周期 Hook 如何处理
  const config: AgentLoopConfig = {
    model: options.model,
    // 自定义/UI 消息不能交给模型；仅保留 Pi 可转换的三种协议消息。
    convertToLlm: (messages) => messages.filter(isModelMessage) as Message[],
    // Runtime 共享本地文件系统边界，强制按 Tool Call 串行执行。
    toolExecution: "sequential",  // 串行，不设置默认parallel
    // Gateway 负责供应商重试；本地 Loop 不叠加 Provider retry。
    maxRetries: 0,
    ...stopHooks,
    beforeToolCall: governedBeforeToolCall,
    // 每轮将旧的 planner snapshot 替换为当前快照，而非 append：模型只看到一个权威计划状态。
    // 准备下一轮使用的最新计划信息
    prepareNextTurn: async (turn) => {
      if (!planStore) return undefined;
      const removedSnapshots = turn.context.messages.filter(isPlanSnapshotMessage).length;
      log(`plan.snapshot.refresh removed=${removedSnapshots} ${describePlan(planStore, planId!)}`);
      return {
        context: {
          ...turn.context,
          messages: [
            ...turn.context.messages.filter((message) => !isPlanSnapshotMessage(message)),
            createPlanSnapshotMessage(planStore, planId!),
          ],
        },
      };
    },
    // 当前没有外部中途指令；自然停止时的 Follow-up 由 stopHooks.getFollowUpMessages 提供。
    getSteeringMessages: async () => [],
  };

  // 唯一模型循环：不在装配层另写 while / for / 递归循环。
  // runAgentLoop(...)
  //   │
  //   ├─ 将初始 user message 加入上下文
  //   │
  //   └─ Pi 内部 runLoop(...)
  //        │
  //        ├─ 调用 streamFn(model, llmContext, { signal, ... })
  //        │
  //        ├─ 接收流式 AssistantMessage
  //        │
  //        ├─ 若 Assistant 有 Tool Call
  //        │    │
  //        │    ├─ 触发 beforeToolCall
  //        │    ├─ 调用 AgentTool.execute(...)
  //        │    ├─ 触发 afterToolCall
  //        │    └─ 把 ToolResult 加回 Context
  //        │
  //        ├─ 触发 turn_end
  //        ├─ 触发 shouldStopAfterTurn
  //        │
  //        ├─ 若继续：
  //        │    ├─ prepareNextTurn
  //        │    ├─ getSteeringMessages
  //        │    └─ 发起下一轮模型请求
  //        │
  //        └─ 若模型自然 Final：
  //             └─ getFollowUpMessages
  const messages = options.resumeMessages
    ? await runAgentLoopContinue(context, config, async (event) => {
      recordEvent(event, trace, () => ++turnCount, options.onTextDelta);
      if (event.type === "turn_start") log(`turn.started number=${turnCount}`);
      if (event.type === "tool_execution_start") log(`tool.executing name=${event.toolName} callId=${event.toolCallId}`);
      if (event.type === "tool_execution_end") log(`tool.finished name=${event.toolName} callId=${event.toolCallId} isError=${event.isError}`);
      if (event.type === "message_end" && event.message.role === "assistant") {
        if (event.message.stopReason === "aborted") { assemblyStopReason ??= AssemblyStopReason.CANCELLED; log(`loop.stop reason=${AssemblyStopReason.CANCELLED} detail=model_stream_aborted`); }
        else if (event.message.stopReason === "error") { assemblyStopReason ??= AssemblyStopReason.MODEL_ERROR; log(`loop.stop reason=${AssemblyStopReason.MODEL_ERROR} detail=model_stream_error`); }
      }
    }, options.signal, options.streamFn)
    : await runAgentLoop(
    [createUserTaskMessage(options.userTask)],
    context,
    config,
    async (event) => {
      // Trace 只取可观测运行事件；文本只转交回调，不采集 thinking_delta。
      recordEvent(event, trace, () => ++turnCount, options.onTextDelta);
      if (event.type === "turn_start") log(`turn.started number=${turnCount}`);
      if (event.type === "tool_execution_start") log(`tool.executing name=${event.toolName} callId=${event.toolCallId}`);
      if (event.type === "tool_execution_end") log(`tool.finished name=${event.toolName} callId=${event.toolCallId} isError=${event.isError}`);

      // 模型流以 aborted / error 收束时，记录装配层原因；Stop Center 已确定的原因不可被覆盖。
      if (event.type === "message_end" && event.message.role === "assistant") {
        if (event.message.stopReason === "aborted") {
          assemblyStopReason ??= AssemblyStopReason.CANCELLED;
          log(`loop.stop reason=${AssemblyStopReason.CANCELLED} detail=model_stream_aborted`);
        } else if (event.message.stopReason === "error") {
          assemblyStopReason ??= AssemblyStopReason.MODEL_ERROR;
          log(`loop.stop reason=${AssemblyStopReason.MODEL_ERROR} detail=model_stream_error`);
        }
      }
    },
    options.signal,
    options.streamFn,
  );

  // 返回不可变快照：Stop Center 原因优先；仅当它未决定时才暴露模型取消/错误原因。
  // stopState 是 Run 结束后，从 RunStopCenter 取出的不可变事实快照：
  // Loop 已经结束
  //    ↓
  // 把 RunStopCenter 内存中积累的状态导出
  //    ↓
  // 供 CLI、API、测试、审计、UI 查看
  const stopState = stopCenter.getState();
  const resolvedStopReason = stopState.stopReason ?? assemblyStopReason;
  log(`run.finished turns=${turnCount} stopReason=${resolvedStopReason ?? "none"} reads=${stopState.readSourceFiles.length} artifacts=${stopState.writtenArtifacts.length}`);
  return Object.freeze({
    state: Object.freeze({
      runId,
      turnCount,
      ...(stopState.stopReason === undefined && assemblyStopReason !== undefined
        ? { stopReason: assemblyStopReason }
        : stopState.stopReason === undefined
          ? {}
          : { stopReason: stopState.stopReason }),
      stopCenter: stopState,
    }),
    messages: Object.freeze([...messages]),
    trace: Object.freeze([...trace]),
    runtime,
  });
}

function describePlan(store: PlanStore, planId: string): string {
  const plan = store.get(planId);
  const statuses = Object.entries(plan.steps.reduce<Record<string, number>>((counts, step) => {
    counts[step.status] = (counts[step.status] ?? 0) + 1;
    return counts;
  }, {})).map(([status, count]) => `${status}:${count}`).join(",");
  // readySteps 由服务端根据 DAG 实时计算。把它写进每次快照日志，可以明确区分：
  // “模型尚未请求下一步工具”与“下一步被依赖/状态机阻塞”。
  const ready = store.readySteps(planId).map((step) => step.id).join(",") || "none";
  return `planId=${plan.id} version=${plan.version} steps={${statuses}} ready=[${ready}] evidence=${plan.evidence.length} revisions=${plan.revisions.length}`;
}

/** 计划快照作为普通 user message 注入；convertToLlm 保留它，避免使用 Pi 不认识的自定义消息类型。 */
function createPlanSnapshotMessage(store: PlanStore, planId: string): AgentMessage {
  const observationIds = store.observationToolCallIds();
  return {
    role: "user",
    content: `[PLAN_STATE] ${store.snapshot(planId)}\n[OBSERVATION_TOOL_CALL_IDS] ${JSON.stringify(observationIds)}\nnote Evidence 的 sourceToolCallIds 必须从这个服务端列表逐字复制；不得自行拼接路径或工具名。`,
    timestamp: Date.now(),
  };
}

function isPlanSnapshotMessage(message: AgentMessage): boolean {
  return message.role === "user"
    && typeof message.content === "string"
    && message.content.startsWith("[PLAN_STATE] ");
}

/** 只构造协议层用户消息；任务文字本身不参与任何权限或路径决策。 */
function createUserTaskMessage(task: string = codebaseAgentTask): AgentMessage {
  return {
    role: "user",
    content: task,
    timestamp: Date.now(),
  };
}

/**
 * Pi StreamFn 只接受基础 Message 联合类型。
 * 自定义/UI 消息若未来加入 AgentContext，必须在此被过滤，避免泄露给模型或导致 provider 转换失败。
 */
function isModelMessage(message: AgentMessage): boolean {
  return message.role === "user" || message.role === "assistant" || message.role === "toolResult";
}

/**
 * 将 Pi 原始事件压缩成调用方需要的三种可审计事件：回合开始、工具开始、工具结束。
 * 只有 text_delta 会向外转发；thinking_delta 不进入 trace，也不交给 UI 回调。
 */
function recordEvent(
  event: AgentEvent,
  trace: AgentTraceEvent[],
  nextTurn: () => number,
  onTextDelta: ((text: string) => void) | undefined,
): void {
  if (event.type === "turn_start") {
    trace.push({ type: "turn_started", turn: nextTurn() });
    return;
  }

  if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
    onTextDelta?.(event.assistantMessageEvent.delta);
    return;
  }

  const turn = trace.filter((entry) => entry.type === "turn_started").length;
  if (event.type === "tool_execution_start") {
    trace.push({
      type: "tool_started",
      turn,
      toolName: event.toolName,
      toolCallId: event.toolCallId,
    });
    return;
  }

  if (event.type === "tool_execution_end") {
    trace.push({
      type: "tool_finished",
      turn,
      toolName: event.toolName,
      toolCallId: event.toolCallId,
      isError: event.isError,
    });
  }
}
