import type {
  AfterToolCallContext,
  BeforeToolCallContext,
  BeforeToolCallResult,
  ShouldStopAfterTurnContext,
} from "@earendil-works/pi-agent-core";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { ToolName, ToolResultCode } from "../runtime/tool-runtime.js";
import type { RuntimeToolDetails } from "../tools/pi-runtime-tools.js";

/** Run 结束的业务原因；一旦确定，采用 first-write-wins，不能被后续事件覆盖。 */
export const StopReason = {
  REPEATED_ACTION: "repeated_action",
  MAX_TURNS_REACHED: "max_turns_reached",
  COMPLETION_CONTRACT_SATISFIED: "completion_contract_satisfied",
} as const;

export type StopReason = (typeof StopReason)[keyof typeof StopReason];

/** 暴露给模型或 Follow-up 的稳定协议标识，不使用实现细节或底层异常文本。 */
export const StopDecisionCode = {
  REPEATED_ACTION: "repeated_action",
  FINAL_REJECTED: "FINAL_REJECTED",
} as const;

/**
 * 自然 Final 被接受前必须满足的最低交付门槛。
 * 此处只声明要求；证据只能在 afterToolCall 收到受控成功结果后进入状态。
 */
export interface CompletionRequirements {
  readonly minimumReadFiles: number;
  readonly requiredArtifactPath: string;
}

/** 创建一个 RunStopCenter 所需的可信 Run 配置。 */
export interface RunStopCenterOptions {
  readonly runId: string;
  readonly maxTurns: number;
  readonly completion: CompletionRequirements;
}

/**
 * 面向装配层与测试的不可变状态快照。
 * 它是 Stop Center 自己记录的事实，不依赖 Pi 内部状态或模型的文字声明。
 */
export interface RunStopState {
  readonly runId: string;
  readonly currentTurn: number;
  readonly maxTurns: number;
  readonly stopReason?: StopReason;
  readonly actionCounts: Readonly<Record<string, number>>;
  readonly readSourceFiles: readonly string[];
  readonly writtenArtifacts: readonly string[];
  readonly pendingFollowUps: readonly AgentMessage[];
}

/**
 * Run 级停止决策中心。
 *
 * 注入 Pi Hook 的功能：在工具执行前阻止第三次重复动作；在工具成功后收集证据；
 * 在每回合结束时判断是否停止；在自然 Final 证据不足时提供 Follow-up。
 * 它不调用模型、不执行工具、不访问文件系统，也不实现 Planner、Checkpoint 或状态机。
 */
export class RunStopCenter {
  readonly #runId: string;
  readonly #maxTurns: number;
  readonly #completion: CompletionRequirements;
  /** key = 工具名 + 递归规范化后的参数；计数在 handler 之前增加。 */
  readonly #actionCounts = new Map<string, number>();
  /** 仅记录 read_file 的成功受控结果对应的模型参数路径。 */
  readonly #readSourceFiles = new Set<string>();
  /** 仅记录 write_artifact 的成功受控结果给出的 artifactPath。 */
  readonly #writtenArtifacts = new Set<string>();
  /** Pi 在自然停止点拉取的用户角色消息；取走即清空，Run 事实不重置。 */
  readonly #pendingFollowUps: AgentMessage[] = [];
  #currentTurn = 0;
  #stopReason: StopReason | undefined;

  constructor(options: RunStopCenterOptions) {
    if (!Number.isInteger(options.maxTurns) || options.maxTurns < 1) {
      throw new Error("maxTurns must be a positive integer.");
    }
    if (!Number.isInteger(options.completion.minimumReadFiles) || options.completion.minimumReadFiles < 1) {
      throw new Error("minimumReadFiles must be a positive integer.");
    }
    if (options.completion.requiredArtifactPath.length === 0) {
      throw new Error("requiredArtifactPath must not be empty.");
    }

    this.#runId = options.runId;
    this.#maxTurns = options.maxTurns;
    this.#completion = Object.freeze({ ...options.completion });
  }

  /**
   * Pi 在 Schema 校验后、AgentTool.execute 前调用。
   * 前两次同一动作放行；第三次不进入 Runtime handler，返回 Pi 可转成错误工具结果的 block。
   */
  beforeToolCall(context: BeforeToolCallContext): BeforeToolCallResult | undefined {
    if (this.#stopReason !== undefined) {
      return blockedResult(this.#stopReason);
    }

    const actionKey = actionFingerprint(context.toolCall.name, context.args);
    const count = (this.#actionCounts.get(actionKey) ?? 0) + 1;
    this.#actionCounts.set(actionKey, count);

    if (count < 3) {
      return undefined;
    }

    this.#setStopReason(StopReason.REPEATED_ACTION);
    return {
      block: true,
      terminate: true,
      reason: `[${StopDecisionCode.REPEATED_ACTION}] The same tool action cannot be executed more than twice in this run.`,
    };
  }

  /**
   * Pi 在工具执行完成后调用。
   * 只消费 Adapter 已治理的成功结果（!isError 且 details.code === ok）；错误结果和模型口头声明均不算证据。
   */
  afterToolCall(context: AfterToolCallContext): void {
    if (context.isError) {
      return;
    }

    const details = asRuntimeToolDetails(context.result.details);
    if (details?.code !== ToolResultCode.OK) {
      return;
    }

    if (context.toolCall.name === ToolName.READ_FILE) {
      const path = asNonEmptyString(context.args, "path");
      if (path !== undefined) this.#readSourceFiles.add(path);
      return;
    }

    if (context.toolCall.name === ToolName.WRITE_ARTIFACT && details.artifactPath !== undefined) {
      this.#writtenArtifacts.add(details.artifactPath);
    }
  }

  /**
   * Pi 在 turn_end 后调用，能看到本轮已追加的工具结果。
   * 判定顺序固定：已有原因 → 最大轮数 → 无工具结果时完成校验 → 继续。
   */
  shouldStopAfterTurn(context: ShouldStopAfterTurnContext): boolean {
    if (this.#stopReason !== undefined) {
      return true;
    }

    this.#currentTurn += 1;
    if (this.#currentTurn >= this.#maxTurns) {
      this.#setStopReason(StopReason.MAX_TURNS_REACHED);
      return true;
    }

    // 没有工具结果表示模型准备自然结束；这不是业务完成，必须检查交付证据。
    if (context.toolResults.length === 0) {
      const missing = this.getMissingCompletionEvidence();
      if (missing.length === 0) {
        this.#setStopReason(StopReason.COMPLETION_CONTRACT_SATISFIED);
        return true;
      }
      this.#pendingFollowUps.push(createFinalRejectedFollowUp(missing));
    }

    return false;
  }

  /** 供 Pi getFollowUpMessages Hook 消费；只清队列，不清计数、轮次或证据。 */
  takeFollowUpMessages(): AgentMessage[] {
    return this.#pendingFollowUps.splice(0);
  }

  /** 返回具体缺失证据，供 Follow-up 精确告知模型下一步要补什么。 */
  getMissingCompletionEvidence(): string[] {
    const missing: string[] = [];
    const remainingReads = this.#completion.minimumReadFiles - this.#readSourceFiles.size;
    if (remainingReads > 0) {
      missing.push(`Read ${remainingReads} more related source file(s); ${this.#readSourceFiles.size}/${this.#completion.minimumReadFiles} successful reads are recorded.`);
    }
    if (!this.#writtenArtifacts.has(this.#completion.requiredArtifactPath)) {
      missing.push(`Write the required login-flow artifact: ${this.#completion.requiredArtifactPath}.`);
    }
    return missing;
  }

  /** 返回排序且冻结的状态，避免调用方修改 Stop Center 内部集合。 */
  getState(): RunStopState {
    return Object.freeze({
      runId: this.#runId,
      currentTurn: this.#currentTurn,
      maxTurns: this.#maxTurns,
      ...(this.#stopReason === undefined ? {} : { stopReason: this.#stopReason }),
      actionCounts: Object.freeze(Object.fromEntries(this.#actionCounts)),
      readSourceFiles: Object.freeze([...this.#readSourceFiles].sort()),
      writtenArtifacts: Object.freeze([...this.#writtenArtifacts].sort()),
      pendingFollowUps: Object.freeze([...this.#pendingFollowUps]),
    });
  }

  /** first-write-wins：后续事件不能把一个已确定的停止原因覆盖成别的原因。 */
  #setStopReason(reason: StopReason): void {
    if (this.#stopReason === undefined) {
      this.#stopReason = reason;
    }
  }
}

/**
 * 将领域 Stop Center 接到 Pi 已安装版本的 Hook 名称上。
 * 这是纯接线层：不新增决策，不改变 Hook 输入，也不创建新的 Run 状态。
 */
export function createRunStopHooks(center: RunStopCenter) {
  return {
    // beforeToolCall
    // • 时机: Pi 完成工具参数 Schema 校验后、真正执行工具前
    // • 作用: 计算动作指纹；第三次相同行为直接阻止，不进入 Runtime handler
    beforeToolCall: async (context: BeforeToolCallContext): Promise<BeforeToolCallResult | undefined> =>
      center.beforeToolCall(context),
    // afterToolCall
    // • 时机: 工具执行结束后
    // • 作用: 只从成功且已治理的结果收集“读了哪些源码”“写了哪些产物”证据
    afterToolCall: async (context: AfterToolCallContext) => {
      center.afterToolCall(context);
      // 不覆盖 Pi 原始工具结果；只让 Center 记录成功证据。
      return undefined;
    },
    // shouldStopAfterTurn
    // • 时机: 一轮模型响应与工具执行结束后
    // • 作用: 按优先级判断：已有停止原因 → 最大轮数 → 自然 Final 的完成契约 → 继续
    shouldStopAfterTurn: async (context: ShouldStopAfterTurnContext): Promise<boolean> =>
      center.shouldStopAfterTurn(context),
    // getFollowUpMessages
    // • 时机: 模型自然结束、但 Loop 准备结束时
    // • 作用: 若 Final 证据不足，取出 [FINAL_REJECTED] 用户 Follow-up，让 Pi 在同一个 Run继续
    getFollowUpMessages: async (): Promise<AgentMessage[]> => center.takeFollowUpMessages(),
  };
}

/** 递归排序对象字段后序列化；对象字段顺序不同但语义相同的调用共享一个动作指纹。 */
export function actionFingerprint(toolName: string, parameters: unknown): string {
  return `${toolName}:${JSON.stringify(canonicalize(parameters))}`;
}

/** 数组顺序保留，对象键递归排序；不对参数做业务语义校验。 */
function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right, "en"))
        .map(([key, nestedValue]) => [key, canonicalize(nestedValue)]),
    );
  }
  return value;
}

/** 仅识别 Adapter 写入的受控 details 形状；未知 details 不可作为证据。 */
function asRuntimeToolDetails(value: unknown): RuntimeToolDetails | undefined {
  if (value === null || typeof value !== "object") return undefined;
  const details = value as Partial<RuntimeToolDetails>;
  return typeof details.code === "string" ? details as RuntimeToolDetails : undefined;
}

/** 从已验证的工具参数中安全取证据路径；读取失败不影响工具结果本身。 */
function asNonEmptyString(value: unknown, key: string): string | undefined {
  if (value === null || typeof value !== "object") return undefined;
  const candidate = (value as Record<string, unknown>)[key];
  return typeof candidate === "string" && candidate.length > 0 ? candidate : undefined;
}

/** 已停止的 Run 再收到工具调用时，统一生成 Pi 可见的终止型错误结果。 */
function blockedResult(reason: StopReason): BeforeToolCallResult {
  return {
    block: true,
    terminate: true,
    reason: `[${reason}] This run has already stopped.`,
  };
}

/** 将缺失项变成同一 Run 的用户 Follow-up；Pi 在自然停止点注入后继续，而非新建循环。 */
function createFinalRejectedFollowUp(missing: readonly string[]): AgentMessage {
  return {
    role: "user",
    content: `[${StopDecisionCode.FINAL_REJECTED}] Delivery evidence is incomplete:\n${missing.map((item) => `- ${item}`).join("\n")}\nContinue using tools to collect the missing evidence and complete the required artifact.`,
    timestamp: Date.now(),
  };
}
