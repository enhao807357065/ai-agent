import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { dirname, relative, resolve, sep } from "node:path";
import { Type, type Static, type TSchema } from "typebox";
import { Check } from "typebox/value";
import type { RunExecutionContext } from "./execution-context.js";
import {
  createPlanParameters,
  type CreatePlanParameters,
  getPlanParameters,
  PlanToolHandlers,
  revisePlanParameters,
  serializePlan,
  updatePlanStepParameters,
} from "../planning/plan-tools.js";
import type { CodeVersionTracker } from "../planning/code-version.js";
import { PlanStore, PlanStoreError } from "../planning/plan-store.js";
import type { RunPlanContext } from "../planning/run-plan-context.js";

// 模型 Tool Call
//   ↓
// Pi AgentTool.execute(...)
//   ↓
// createPiRuntimeTools(...)
//   ↓
// ToolRuntime.execute({
//   toolCallId,
//   toolName: "search_code",
//   modelArguments,
//   context,
//   signal
// })
//   ↓
// 1. 从 #tools 查工具注册表
// 2. TypeBox Schema 校验参数
// 3. constrainParameters 路径约束
// 4. 调用 searchCode handler
// 5. Node readdir/readFile 实际读文件
// 6. 组装 ControlledToolResult
//   ↓
// Pi ToolResultMessage
//   ↓
// 下一轮模型 Context

export const ToolName = {
  LIST_FILES: "list_files",
  SEARCH_CODE: "search_code",
  READ_FILE: "read_file",
  WRITE_ARTIFACT: "write_artifact",
  /** 受控的精确文本替换：只允许在 repositoryRoot 内编辑已有文本源码/配置文件。 */
  MODIFY_CODE: "modify_code",
  /** 原子多文件补丁：所有唯一匹配先校验通过，才一次性写入全部目标。 */
  APPLY_PATCH: "apply_patch",
  /** 受控测试入口：只允许执行固定 npm scripts，不能接收任意 shell 命令。 */
  RUN_TESTS: "run_tests",
  CREATE_PLAN: "create_plan",
  GET_PLAN: "get_plan",
  UPDATE_PLAN_STEP: "update_plan_step",
  REVISE_PLAN: "revise_plan",
} as const;

export type ToolName = (typeof ToolName)[keyof typeof ToolName];

export const ToolResultCode = {
  OK: "ok",
  TOOL_NOT_FOUND: "tool_not_found",
  /** 路径已在受信根内，但目标不存在；与越权路径或内部执行异常分开反馈。 */
  NOT_FOUND: "not_found",
  INVALID_ARGUMENT: "invalid_argument",
  PATH_REJECTED: "path_rejected",
  ARTIFACT_PATH_REJECTED: "artifact_path_rejected",
  TEST_COMMAND_REJECTED: "test_command_rejected",
  TEST_FAILED: "test_failed",
  CANCELLED: "cancelled",
  EXECUTION_FAILED: "execution_failed",
} as const;

export type ToolResultCode = (typeof ToolResultCode)[keyof typeof ToolResultCode];
type FailureToolResultCode = Exclude<ToolResultCode, "ok">;

/** 仅包含已治理内容；未来 Pi adapter 只能把此对象映射为 toolResult。 */
export interface ControlledToolResult {
  readonly toolCallId: string;
  readonly success: boolean;
  readonly code: ToolResultCode;
  readonly content: string;
  readonly artifactPath?: string;
  /** Runtime 可根据受控执行结果产生草稿；step/call/version 由 afterToolCall 绑定。 */
  readonly evidenceDraft?: import("../planning/plan-store.js").EvidenceDraft;
  /** 成功测试实际覆盖的 workspace 摘要；模型不能自行声明。 */
  readonly verification?: { readonly scope: string; readonly testedDigest: string };
}

export interface ToolExecutionRequest {
  readonly toolCallId: string;
  readonly toolName: string;
  readonly modelArguments: unknown;
  readonly context: RunExecutionContext;
  readonly signal?: AbortSignal;
}

const optionalStartPath = Type.Optional(Type.String({ minLength: 1 }));
const listFilesParameters = Type.Object(
  { startPath: optionalStartPath },
  { additionalProperties: false },
);
const searchCodeParameters = Type.Object(
  {
    query: Type.String({ minLength: 1 }),
    startPath: optionalStartPath,
  },
  { additionalProperties: false },
);
const readFileParameters = Type.Object(
  { path: Type.String({ minLength: 1 }) },
  { additionalProperties: false },
);
const writeArtifactParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
    content: Type.String({ minLength: 1 }),
  },
  { additionalProperties: false },
);
const modifyCodeParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
    oldText: Type.String({ minLength: 1 }),
    newText: Type.String(),
  },
  { additionalProperties: false },
);
const applyPatchParameters = Type.Object(
  {
    patches: Type.Array(Type.Object({
      path: Type.String({ minLength: 1 }),
      oldText: Type.String({ minLength: 1 }),
      newText: Type.String(),
    }, { additionalProperties: false }), { minItems: 1, maxItems: 20 }),
  },
  { additionalProperties: false },
);
const runTestsParameters = Type.Object(
  { script: Type.String({ minLength: 1 }) },
  { additionalProperties: false },
);

type ListFilesParameters = Static<typeof listFilesParameters>;
type SearchCodeParameters = Static<typeof searchCodeParameters>;
type ReadFileParameters = Static<typeof readFileParameters>;
type WriteArtifactParameters = Static<typeof writeArtifactParameters>;
type ModifyCodeParameters = Static<typeof modifyCodeParameters>;
type ApplyPatchParameters = Static<typeof applyPatchParameters>;
type RunTestsParameters = Static<typeof runTestsParameters>;

/** 模型只能请求这些 package.json 脚本；禁止传递任意 shell 片段或参数。 */
const allowedTestScripts = new Set(["test", "build"]);

const skippedDirectoryNames = new Set([".git", "node_modules"]);
const searchableExtensions = new Set([
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".json",
  ".md",
  ".yml",
  ".yaml",
  ".toml",
  ".ini",
  ".env",
  ".html",
  ".css",
  ".sql",
]);

class KnownToolError extends Error {
  constructor(
    readonly code: FailureToolResultCode,
    message: string,
  ) {
    super(message);
  }
}

/** 测试失败仍是可信执行事实：保留实际受测内容摘要，供 Run 层使旧 pass 失效。 */
class VerificationFailedError extends KnownToolError {
  constructor(
    message: string,
    readonly verification: { readonly scope: string; readonly testedDigest: string },
  ) {
    super(ToolResultCode.TEST_FAILED, message);
  }
}

type ToolHandler = (
  parameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
) => Promise<HandlerOutput>;

interface HandlerOutput {
  readonly content: string;
  readonly artifactPath?: string;
  /** 只有实际测试进程成功退出后才存在，摘要取自启动前的真实受测工作区。 */
  readonly verification?: { readonly scope: string; readonly testedDigest: string };
}

interface RegisteredTool {
  readonly schema: TSchema;
  readonly handler: ToolHandler;
}

/**
 * 唯一的本地工具执行入口。
 *
 * 该 Runtime 不调用模型，也不适配 Pi；未来 adapter 必须经由 execute() 获取
 * ControlledToolResult，禁止直接调用任意 handler。
 */
export interface ToolRuntimeOptions {
  /**
   * 可注入同一个 PlanStore，供同一进程内多个调用共享计划状态。
   * 未注入时 Runtime 创建一个仅属于自身的内存 Store，适合当前教学 Demo。
   */
  readonly planStore?: PlanStore;
  /** create_plan 成功后由 Runtime 激活当前 Run 的唯一 Plan，不允许模型静默切换。 */
  readonly runPlanContext?: RunPlanContext;
  readonly codeVersion?: string;
  /** 可变版本源仅由服务端 Runtime 在 modify_code 成功后推进。 */
  readonly codeVersionTracker?: CodeVersionTracker;
  /** 模型只能请求这些 package.json 脚本；禁止传递任意 shell 片段或参数。 */
  readonly allowedTestScripts?: readonly string[];
  /** 测试进程的服务端超时上限。 */
  readonly testTimeoutMs?: number;
}

export class ToolRuntime {
  readonly #tools: ReadonlyMap<string, RegisteredTool>;
  readonly #handlerExecutionCounts = new Map<string, number>();
  readonly #verificationOutcomes = new Map<string, { readonly scope: string; readonly testedDigest: string; readonly passed: boolean; readonly failureCode?: string }>();
  readonly #allowedTestScripts: ReadonlySet<string>;
  readonly #testTimeoutMs: number;
  readonly #codeVersionTracker: CodeVersionTracker | undefined;
  readonly #runPlanContext: RunPlanContext | undefined;
  /** Plan handler 也必须经本 Runtime 注册；它不是第二个可供 Agent 直调的执行入口。 */
  readonly #planHandlers: PlanToolHandlers;

  constructor(options: ToolRuntimeOptions = {}) {
    this.#allowedTestScripts = new Set(options.allowedTestScripts ?? allowedTestScripts);
    this.#testTimeoutMs = options.testTimeoutMs ?? 60_000;
    this.#codeVersionTracker = options.codeVersionTracker;
    this.#runPlanContext = options.runPlanContext;
    if (!Number.isInteger(this.#testTimeoutMs) || this.#testTimeoutMs < 1) {
      throw new Error("testTimeoutMs must be a positive integer.");
    }
    // 注入已有 Store 时保持其版本源、Verifier 与计划状态；否则创建最小单进程版本。
    this.#planHandlers = new PlanToolHandlers(
      options.planStore ?? new PlanStore(options.codeVersion ?? "in-memory-current"),
    );
    this.#tools = new Map<string, RegisteredTool>([
      [ToolName.LIST_FILES, { schema: listFilesParameters, handler: listFiles }],
      [ToolName.SEARCH_CODE, { schema: searchCodeParameters, handler: searchCode }],
      [ToolName.READ_FILE, { schema: readFileParameters, handler: readSourceFile }],
      [ToolName.WRITE_ARTIFACT, { schema: writeArtifactParameters, handler: writeAnalysisArtifact }],
      [ToolName.MODIFY_CODE, { schema: modifyCodeParameters, handler: modifyCode }],
      [ToolName.APPLY_PATCH, { schema: applyPatchParameters, handler: applyPatch }],
      [ToolName.RUN_TESTS, { schema: runTestsParameters, handler: (input, context, signal) => runTests(input, context, signal, this.#allowedTestScripts, this.#testTimeoutMs) }],
      // Planner 工具与文件工具共享 schema → Runtime → controlled result 时序。
      // `as never` 只处理动态注册表的 unknown 边界；schema 已在 execute() 中先校验。
      [ToolName.CREATE_PLAN, { schema: createPlanParameters, handler: async (input) => ({
        content: serializePlan(this.#planHandlers.createPlan(input as never)),
      }) }],
      [ToolName.GET_PLAN, { schema: getPlanParameters, handler: async (input) => ({
        content: serializePlan(this.#planHandlers.getPlan(input as never)),
      }) }],
      [ToolName.UPDATE_PLAN_STEP, { schema: updatePlanStepParameters, handler: async (input) => ({
        content: serializePlan(this.#planHandlers.updatePlanStep(input as never)),
      }) }],
      [ToolName.REVISE_PLAN, { schema: revisePlanParameters, handler: async (input) => ({
        content: serializePlan(this.#planHandlers.revisePlan(input as never)),
      }) }],
    ]);
  }

  getHandlerExecutionCount(toolName: string): number {
    return this.#handlerExecutionCounts.get(toolName) ?? 0;
  }

  /** 失败的测试也要留下受控事实，Hook 才能让最新失败覆盖旧 pass。 */
  getVerificationOutcome(toolCallId: string): { readonly scope: string; readonly testedDigest: string; readonly passed: boolean; readonly failureCode?: string } | undefined {
    return this.#verificationOutcomes.get(toolCallId);
  }

  async execute(request: ToolExecutionRequest): Promise<ControlledToolResult> {
    const tool = this.#tools.get(request.toolName);
    if (!tool) {
      return failureResult(
        request.toolCallId,
        ToolResultCode.TOOL_NOT_FOUND,
        "The requested tool is not registered.",
      );
    }

    if (request.signal?.aborted) {
      return failureResult(request.toolCallId, ToolResultCode.CANCELLED, "Tool execution was cancelled.");
    }

    if (!isRecord(request.modelArguments) || !Check(tool.schema, request.modelArguments)) {
      return failureResult(
        request.toolCallId,
        ToolResultCode.INVALID_ARGUMENT,
        "Tool arguments do not match the required schema.",
      );
    }

    try {
      // create_plan 是唯一会建立活跃 Plan 的模型工具。先拒绝第二次创建，避免先把 B
      // 写入 Store、随后才发现不能 activate 而留下无主 Plan。
      if (request.toolName === ToolName.CREATE_PLAN && this.#runPlanContext?.hasActivePlan()) {
        throw new PlanStoreError("PLAN_ALREADY_ACTIVE", "This run already has an active plan.");
      }
      const constrainedParameters = constrainParameters(
        request.toolName,
        request.modelArguments,
        request.context,
      );
      this.#handlerExecutionCounts.set(
        request.toolName,
        this.getHandlerExecutionCount(request.toolName) + 1,
      );
      const output = await tool.handler(constrainedParameters, request.context, request.signal);
      // create_plan 的领域创建已成功后才绑定为当前 Run 的活跃 Plan；activation 失败会被
      // 归一化为受控错误，不能让第二个 Plan 静默替换已有治理对象。
      if (request.toolName === ToolName.CREATE_PLAN && this.#runPlanContext) {
        const parameters = request.modelArguments as CreatePlanParameters;
        this.#runPlanContext.activate(parameters.planId);
      }
      // 只有真实代码写入成功返回后才推进版本；失败/拒绝不得污染版本。
      if (request.toolName === ToolName.MODIFY_CODE || request.toolName === ToolName.APPLY_PATCH) this.#codeVersionTracker?.advanceAfterCodeChange();
      if (request.toolName === ToolName.RUN_TESTS && output.verification) {
        this.#verificationOutcomes.set(request.toolCallId, { ...output.verification, passed: true });
      }
      return successResult(
        request.toolCallId,
        output.content,
        output.artifactPath,
        evidenceDraftFor(request.toolName, output),
        output.verification,
      );
    } catch (error) {
      // 文件已通过可信根路径约束但在 handler 读取时不存在，是业务可恢复事实，不应坍缩为
      // execution_failed；模型可据此调整路径，Harness 也不会把它记录成正向 Evidence。
      if (isMissingFileError(error)) {
        return failureResult(request.toolCallId, ToolResultCode.NOT_FOUND, "The requested file does not exist within the permitted repository.");
      }
      if (error instanceof VerificationFailedError) {
        this.#verificationOutcomes.set(request.toolCallId, { ...error.verification, passed: false, failureCode: error.code });
        return failureResult(request.toolCallId, error.code, error.message, error.verification);
      }
      if (error instanceof KnownToolError) {
        return failureResult(request.toolCallId, error.code, error.message);
      }
      if (error instanceof PlanStoreError) {
        return failureResult(request.toolCallId, ToolResultCode.INVALID_ARGUMENT, `[${error.code}] ${error.message}`);
      }
      return failureResult(
        request.toolCallId,
        ToolResultCode.EXECUTION_FAILED,
        "Tool execution failed.",
      );
    }
  }
}

function isMissingFileError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && (error as { code?: unknown }).code === "ENOENT";
}

function constrainParameters(
  toolName: string,
  rawParameters: unknown,
  context: RunExecutionContext,
): unknown {
  switch (toolName) {
    case ToolName.LIST_FILES: {
      const parameters = rawParameters as ListFilesParameters;
      return Object.freeze({
        ...parameters,
        ...(parameters.startPath === undefined
          ? {}
          : { startPath: resolveRepositoryPath(context.repositoryRoot, parameters.startPath) }),
      });
    }
    case ToolName.SEARCH_CODE: {
      const parameters = rawParameters as SearchCodeParameters;
      return Object.freeze({
        ...parameters,
        ...(parameters.startPath === undefined
          ? {}
          : { startPath: resolveRepositoryPath(context.repositoryRoot, parameters.startPath) }),
      });
    }
    case ToolName.READ_FILE: {
      const parameters = rawParameters as ReadFileParameters;
      return Object.freeze({
        path: resolveRepositoryPath(context.repositoryRoot, parameters.path),
      });
    }
    case ToolName.WRITE_ARTIFACT: {
      const parameters = rawParameters as WriteArtifactParameters;
      return Object.freeze({
        content: parameters.content,
        path: resolveArtifactPath(context.artifactsRoot, parameters.path),
      });
    }
    case ToolName.MODIFY_CODE: {
      const parameters = rawParameters as ModifyCodeParameters;
      return Object.freeze({
        oldText: parameters.oldText,
        newText: parameters.newText,
        path: resolveRepositoryPath(context.repositoryRoot, parameters.path),
      });
    }
    case ToolName.APPLY_PATCH: {
      const parameters = rawParameters as ApplyPatchParameters;
      const seen = new Set<string>();
      return Object.freeze({
        patches: Object.freeze(parameters.patches.map((patch) => {
          const path = resolveRepositoryPath(context.repositoryRoot, patch.path);
          if (!seen.add(path)) {
            throw new KnownToolError(ToolResultCode.INVALID_ARGUMENT, "A patch may edit each file at most once; combine same-file changes into one unique replacement.");
          }
          return Object.freeze({ path, oldText: patch.oldText, newText: patch.newText });
        })),
      });
    }
    case ToolName.RUN_TESTS: {
      const parameters = rawParameters as RunTestsParameters;
      return Object.freeze({ script: parameters.script });
    }
    /**
     * Planning 参数没有路径语义，因此无需走文件路径约束。
     * 但这不表示它们已可信：下一层 PlanToolHandlers / PlanStore 仍会校验
     * 状态机、证据归属、乐观锁与 DAG；此处仅复制为冻结对象，避免 handler 改写原始输入。
     */
    case ToolName.CREATE_PLAN:
    case ToolName.GET_PLAN:
    case ToolName.UPDATE_PLAN_STEP:
    case ToolName.REVISE_PLAN:
      return Object.freeze({ ...(rawParameters as Record<string, unknown>) });
    default:
      throw new KnownToolError(ToolResultCode.TOOL_NOT_FOUND, "The requested tool is not registered.");
  }
}

function evidenceDraftFor(
  toolName: string,
  output: HandlerOutput,
): import("../planning/plan-store.js").EvidenceDraft | undefined {
  // Runtime 只陈述“这个受控工具结果可作为哪类证据”，没有 stepId、callId 或 codeVersion。
  // 这些身份/版本事实只能在 Pi afterToolCall 看到实际成功结果后由装配层补齐。
  switch (toolName) {
    case ToolName.WRITE_ARTIFACT:
    case ToolName.MODIFY_CODE:
    case ToolName.APPLY_PATCH:
      return { kind: "change", summary: output.artifactPath ?? output.content };
    case ToolName.RUN_TESTS:
      return { kind: "test", summary: output.content };
    default:
      return undefined;
  }
}

function successResult(
  toolCallId: string,
  content: string,
  artifactPath?: string,
  evidenceDraft?: import("../planning/plan-store.js").EvidenceDraft,
  verification?: { readonly scope: string; readonly testedDigest: string },
): ControlledToolResult {
  return Object.freeze({
    toolCallId,
    success: true,
    code: ToolResultCode.OK,
    content,
    ...(artifactPath === undefined ? {} : { artifactPath }),
    ...(evidenceDraft === undefined ? {} : { evidenceDraft }),
    ...(verification === undefined ? {} : { verification }),
  });
}

function failureResult(
  toolCallId: string,
  code: FailureToolResultCode,
  content: string,
  verification?: { readonly scope: string; readonly testedDigest: string },
): ControlledToolResult {
  return Object.freeze({
    toolCallId,
    success: false,
    code,
    content,
    ...(verification === undefined ? {} : { verification }),
  });
}

async function listFiles(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as ListFilesParameters;
  const start = parameters.startPath ?? context.repositoryRoot;
  const files: string[] = [];
  await collectFiles(context.repositoryRoot, start, files, signal);
  return {
    content: files
      .map((file) => toRepositoryRelativePath(context.repositoryRoot, file))
      .sort(comparePaths)
      .join("\n") || ".",
  };
}

async function searchCode(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as SearchCodeParameters;
  const start = parameters.startPath ?? context.repositoryRoot;
  const files: string[] = [];
  await collectFiles(context.repositoryRoot, start, files, signal);

  const matches: string[] = [];
  for (const file of files.sort(comparePaths)) {
    throwIfAborted(signal);
    if (!isSearchableTextFile(file)) continue;

    const content = await readFile(file, "utf8");
    const relativePath = toRepositoryRelativePath(context.repositoryRoot, file);
    for (const [index, line] of content.split(/\r?\n/u).entries()) {
      throwIfAborted(signal);
      if (line.includes(parameters.query)) {
        matches.push(`${relativePath}:${index + 1}:${line}`);
      }
    }
  }

  return { content: matches.join("\n") || "No matches found." };
}

async function readSourceFile(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as ReadFileParameters;
  throwIfAborted(signal);
  const sourcePath = parameters.path;
  const content = await readFile(sourcePath, "utf8");
  throwIfAborted(signal);
  return {
    content: `${toRepositoryRelativePath(context.repositoryRoot, sourcePath)}\n${content}`,
  };
}

async function writeAnalysisArtifact(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as WriteArtifactParameters;
  throwIfAborted(signal);
  const artifactPath = parameters.path;
  await mkdir(dirname(artifactPath), { recursive: true });
  await writeFile(artifactPath, parameters.content, "utf8");
  throwIfAborted(signal);
  return {
    content: `Analysis artifact written: ${toArtifactRelativePath(context.artifactsRoot, artifactPath)}`,
    artifactPath: toArtifactRelativePath(context.artifactsRoot, artifactPath),
  };
}

// modify_code
// • 参数: path、oldText、newText、planStepId
// • 做什么: 对仓库内已有文本源码/配置文件做唯一、精确文本替换
// • 成功产出: change Evidence
async function modifyCode(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as ModifyCodeParameters;
  throwIfAborted(signal);
  if (!isSearchableTextFile(parameters.path)) {
    throw new KnownToolError(ToolResultCode.PATH_REJECTED, "Only supported text source and configuration files may be modified.");
  }
  const before = await readFile(parameters.path, "utf8");
  const occurrences = before.split(parameters.oldText).length - 1;
  if (occurrences !== 1) {
    throw new KnownToolError(
      ToolResultCode.INVALID_ARGUMENT,
      occurrences === 0 ? "oldText was not found in the requested file." : "oldText must match exactly once; read the file and provide more context.",
    );
  }
  throwIfAborted(signal);
  await writeFile(parameters.path, before.replace(parameters.oldText, parameters.newText), "utf8");
  throwIfAborted(signal);
  const path = toRepositoryRelativePath(context.repositoryRoot, parameters.path);
  return { content: `Code modified: ${path}`, artifactPath: path };
}

async function applyPatch(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
): Promise<HandlerOutput> {
  const parameters = rawParameters as ApplyPatchParameters;
  throwIfAborted(signal);

  // Phase 1: read and validate every hunk. No write occurs until the entire
  // patch has a deterministic target, giving this multi-file operation an
  // all-or-nothing semantic for all validation failures.
  const prepared: Array<{ readonly path: string; readonly after: string }> = [];
  for (const patch of parameters.patches) {
    if (!isSearchableTextFile(patch.path)) {
      throw new KnownToolError(ToolResultCode.PATH_REJECTED, "Only supported text source and configuration files may be patched.");
    }
    const before = await readFile(patch.path, "utf8");
    const occurrences = before.split(patch.oldText).length - 1;
    if (occurrences !== 1) {
      throw new KnownToolError(
        ToolResultCode.INVALID_ARGUMENT,
        occurrences === 0
          ? "A patch oldText was not found in the requested file; re-read it before retrying."
          : "Each patch oldText must match exactly once; provide more context.",
      );
    }
    prepared.push({ path: patch.path, after: before.replace(patch.oldText, patch.newText) });
  }

  throwIfAborted(signal);
  // Phase 2: all validated writes. apply_patch intentionally supports only
  // modifications to existing text files: no create/delete/rename, binary,
  // symlink, mode-change, shell command, or path traversal capability.
  for (const patch of prepared) {
    await writeFile(patch.path, patch.after, "utf8");
  }
  throwIfAborted(signal);
  const files = prepared.map((patch) => toRepositoryRelativePath(context.repositoryRoot, patch.path));
  return { content: `Patch applied atomically to: ${files.join(", ")}`, artifactPath: files.join(", ") };
}

// run_tests
// • 参数: script、planStepId
// • 做什么: 执行服务端许可的 npm run <script>
// • 成功产出: test Evidence
async function runTests(
  rawParameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
  allowedScripts: ReadonlySet<string>,
  timeoutMs: number,
): Promise<HandlerOutput> {
  const parameters = rawParameters as RunTestsParameters;
  if (!allowedScripts.has(parameters.script)) {
    throw new KnownToolError(ToolResultCode.TEST_COMMAND_REJECTED, "Only server-approved package scripts may be run.");
  }
  const testedDigest = await repositoryDigest(context.repositoryRoot, signal);
  const outcome = await runNpmScript(context.repositoryRoot, parameters.script, signal, timeoutMs);
  const summary = `npm run ${parameters.script}: exitCode=${outcome.exitCode}${outcome.timedOut ? " timedOut=true" : ""}`;
  if (outcome.exitCode !== 0 || outcome.timedOut) {
    throw new VerificationFailedError(summary, { scope: `npm:${parameters.script}`, testedDigest });
  }
  return { content: summary, verification: { scope: `npm:${parameters.script}`, testedDigest } };
}

async function runNpmScript(
  cwd: string,
  script: string,
  signal: AbortSignal | undefined,
  timeoutMs: number,
): Promise<{ readonly exitCode: number; readonly timedOut: boolean }> {
  throwIfAborted(signal);
  return await new Promise((resolvePromise, reject) => {
    const child = spawn("npm", ["run", script], { cwd, shell: false, stdio: "ignore" });
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill("SIGTERM"); }, timeoutMs);
    const abort = (): void => { child.kill("SIGTERM"); };
    signal?.addEventListener("abort", abort, { once: true });
    child.once("error", reject);
    child.once("close", (code) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (signal?.aborted) {
        reject(new KnownToolError(ToolResultCode.CANCELLED, "Tool execution was cancelled."));
        return;
      }
      resolvePromise({ exitCode: code ?? 1, timedOut });
    });
  });
}

async function repositoryDigest(root: string, signal: AbortSignal | undefined): Promise<string> {
  const files: string[] = [];
  await collectFiles(root, root, files, signal);
  const manifest: string[] = [];
  for (const file of files.sort(comparePaths)) {
    throwIfAborted(signal);
    manifest.push(`${toRepositoryRelativePath(root, file)}:${createHash("sha256").update(await readFile(file)).digest("hex")}`);
  }
  return createHash("sha256").update(manifest.join("\n")).digest("hex");
}

async function collectFiles(
  repositoryRoot: string,
  currentPath: string,
  files: string[],
  signal: AbortSignal | undefined,
): Promise<void> {
  throwIfAborted(signal);
  const entries = await readdir(currentPath, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => comparePaths(left.name, right.name))) {
    throwIfAborted(signal);
    const entryPath = resolve(currentPath, entry.name);
    assertWithinRoot(repositoryRoot, entryPath, ToolResultCode.PATH_REJECTED);

    if (entry.isDirectory()) {
      if (!skippedDirectoryNames.has(entry.name)) {
        await collectFiles(repositoryRoot, entryPath, files, signal);
      }
      continue;
    }

    if (entry.isFile()) files.push(entryPath);
  }
}

function resolveRepositoryPath(repositoryRoot: string, requestedPath?: string): string {
  const resolvedPath = resolve(repositoryRoot, requestedPath ?? ".");
  assertWithinRoot(repositoryRoot, resolvedPath, ToolResultCode.PATH_REJECTED);
  return resolvedPath;
}

function resolveArtifactPath(artifactsRoot: string, requestedPath: string): string {
  const resolvedPath = resolve(artifactsRoot, requestedPath);
  assertWithinRoot(artifactsRoot, resolvedPath, ToolResultCode.ARTIFACT_PATH_REJECTED);
  return resolvedPath;
}

function assertWithinRoot(
  root: string,
  candidate: string,
  code: typeof ToolResultCode.PATH_REJECTED | typeof ToolResultCode.ARTIFACT_PATH_REJECTED,
): void {
  const rootPath = resolve(root);
  const relativePath = relative(rootPath, candidate);
  if (relativePath === "" || (!relativePath.startsWith(`..${sep}`) && relativePath !== ".." && !relativePath.includes(`..${sep}`))) {
    return;
  }
  throw new KnownToolError(code, "The requested path is outside the permitted root.");
}

function toRepositoryRelativePath(repositoryRoot: string, absolutePath: string): string {
  const path = relative(resolve(repositoryRoot), absolutePath);
  return path === "" ? "." : path.split(sep).join("/");
}

function toArtifactRelativePath(artifactsRoot: string, absolutePath: string): string {
  return relative(resolve(artifactsRoot), absolutePath).split(sep).join("/") || ".";
}

function isSearchableTextFile(filePath: string): boolean {
  const lastSegment = filePath.slice(filePath.lastIndexOf(sep) + 1);
  if (lastSegment === ".env") return true;
  const dot = lastSegment.lastIndexOf(".");
  return dot >= 0 && searchableExtensions.has(lastSegment.slice(dot));
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) {
    throw new KnownToolError(ToolResultCode.CANCELLED, "Tool execution was cancelled.");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function comparePaths(left: string, right: string): number {
  return left.localeCompare(right, "en");
}
