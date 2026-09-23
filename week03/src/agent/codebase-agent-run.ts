import {
  runAgentLoop,
  type AgentContext,
  type AgentEvent,
  type AgentLoopConfig,
  type AgentMessage,
  type StreamFn,
} from "@earendil-works/pi-agent-core";
import type { Message, Model } from "@earendil-works/pi-ai";
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

/** 每次模型请求都会携带的系统约束；用于引导工具使用，不承担任何安全校验职责。 */
export const codebaseAgentSystemPrompt = [
  "你是一个代码理解 Agent。",
  "形成结论前，必须先搜索并阅读源码；不得仅根据文件名猜测实现。",
  "分析目标仓库的登录逻辑，并生成 README 要求的登录流程 Markdown 说明产物。",
  "最终回答必须列出实际阅读并引用过的源码相对路径。",
].join("\n");

/** 作为 runAgentLoop 初始 prompt 进入 Context 的固定用户任务。 */
export const codebaseAgentTask =
  "分析目标仓库的登录逻辑，并生成 Markdown 登录流程说明文档。";

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
  readonly completion?: CompletionRequirements;
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

  // Stop Center 不参与执行；它只在 Pi Hooks 时根据本 Run 已知事实作决定。
  const stopCenter = new RunStopCenter({
    runId,
    maxTurns: options.maxTurns ?? defaultMaxTurns,
    completion: options.completion ?? {
      ...defaultCompletion,
      requiredArtifactPath: executionContext.taskArtifactPath,
    },
  });
  // taskArtifactPath 是执行上下文的可信配置，优先于硬编码的教学默认文件名。
  const tools = createPiRuntimeTools(runtime, executionContext);
  const trace: AgentTraceEvent[] = [];
  let turnCount = 0;
  let assemblyStopReason: AssemblyStopReason | undefined;

  // AgentContext 决定“模型可看到什么”：Prompt 与 Adapter 产出的工具，不包含 Runtime 内部对象。
  const context: AgentContext = {
    systemPrompt: codebaseAgentSystemPrompt,
    messages: [],
    tools,
  };

  // 复用 Stop Center 的 Hook；装配层只接线，不在这里复制重复动作、证据或 Final 规则。
  const stopHooks = createRunStopHooks(stopCenter);
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
    // 仅预留下一轮准备扩展点；当前不保存 Checkpoint，也不篡改 Stop Center 证据。
    // 触发时机：上一轮完整结束
    // → shouldStopAfterTurn 返回 false
    // → 下一轮真正开始之前
    prepareNextTurn: async () => undefined,
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
  const messages = await runAgentLoop(
    [createUserTaskMessage()],
    context,
    config,
    async (event) => {
      // Trace 只取可观测运行事件；文本只转交回调，不采集 thinking_delta。
      recordEvent(event, trace, () => ++turnCount, options.onTextDelta);

      // 模型流以 aborted / error 收束时，记录装配层原因；Stop Center 已确定的原因不可被覆盖。
      if (event.type === "message_end" && event.message.role === "assistant") {
        if (event.message.stopReason === "aborted") {
          assemblyStopReason ??= AssemblyStopReason.CANCELLED;
        } else if (event.message.stopReason === "error") {
          assemblyStopReason ??= AssemblyStopReason.MODEL_ERROR;
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

/** 只构造协议层用户消息；任务文字本身不参与任何权限或路径决策。 */
function createUserTaskMessage(): AgentMessage {
  return {
    role: "user",
    content: codebaseAgentTask,
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
