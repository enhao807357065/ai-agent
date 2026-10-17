import type { AgentTool, AgentToolResult } from "@earendil-works/pi-agent-core";
import { Type, type Static } from "typebox";
import {
  createPlanParameters,
  getPlanParameters,
  revisePlanParameters,
  updatePlanStepParameters,
} from "../planning/plan-tools.js";
import type { RunExecutionContext } from "../runtime/execution-context.js";
import {
  ToolName,
  type ControlledToolResult,
  type ToolExecutionRequest,
  type ToolRuntime,
} from "../runtime/tool-runtime.js";

const planStepId = Type.Optional(Type.String({ minLength: 1 }));

// planStepId is loop-governance metadata. Pi validates it and beforeToolCall enforces it
// for state-changing tools; the thin adapter removes it before the only Runtime entrance.
const listFilesParameters = Type.Object(
  {
    startPath: Type.Optional(Type.String({ minLength: 1 })),
    planStepId,
  },
  { additionalProperties: false },
);

const searchCodeParameters = Type.Object(
  {
    query: Type.String({ minLength: 1 }),
    startPath: Type.Optional(Type.String({ minLength: 1 })),
    planStepId,
  },
  { additionalProperties: false },
);

const readFileParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
    planStepId,
  },
  { additionalProperties: false },
);

const writeArtifactParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
    content: Type.String({ minLength: 1 }),
    planStepId,
  },
  { additionalProperties: false },
);
const modifyCodeParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }), oldText: Type.String({ minLength: 1 }), newText: Type.String(), planStepId,
  },
  { additionalProperties: false },
);
const applyPatchParameters = Type.Object(
  {
    patches: Type.Array(Type.Object({
      path: Type.String({ minLength: 1 }), oldText: Type.String({ minLength: 1 }), newText: Type.String(),
    }, { additionalProperties: false }), { minItems: 1, maxItems: 20 }),
    planStepId,
  },
  { additionalProperties: false },
);
const runTestsParameters = Type.Object(
  { script: Type.String({ minLength: 1 }), planStepId },
  { additionalProperties: false },
);

type RuntimeExecutor = Pick<ToolRuntime, "execute">;

import type { EvidenceDraft } from "../planning/plan-store.js";

export interface RuntimeToolDetails {
  readonly code: ControlledToolResult["code"];
  readonly artifactPath?: string;
  /** 仅是 Runtime 草稿；PlanStore 的真实 Evidence 在 afterToolCall 才写入。 */
  readonly evidenceDraft?: EvidenceDraft;
  /** 实际成功测试的 scope 与启动前工作区摘要。 */
  readonly verification?: ControlledToolResult["verification"];
}

/**
 * Pi 的错误工具结果由 AgentTool.execute() 抛出的 Error 生成。
 * 将 Runtime 已治理的错误码保留在 Error.code 和模型可读的 message 中。
 */
export class RuntimeToolFailure extends Error {
  readonly code: Exclude<ControlledToolResult["code"], "ok">;

  constructor(result: ControlledToolResult) {
    super(`[${result.code}] ${result.content}`);
    this.name = "RuntimeToolFailure";
    this.code = result.code as Exclude<ControlledToolResult["code"], "ok">;
  }
}

/**
 * 仅把 Pi 调用适配到唯一 ToolRuntime：不访问文件系统、不做路径/语义校验、不重试。
 */
export function createPiRuntimeTools(
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool[] {
  return [
    createRuntimeTool(
      ToolName.LIST_FILES,
      "List files available in the project so you can locate relevant source or configuration files.",
      "List files",
      listFilesParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.SEARCH_CODE,
      "Search project text files for an exact query when you need to find relevant implementation details.",
      "Search code",
      searchCodeParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.READ_FILE,
      "Read one project file after identifying the file whose contents are needed.",
      "Read file",
      readFileParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.WRITE_ARTIFACT,
      "Write the completed analysis artifact after you have prepared its final content.",
      "Write artifact",
      writeArtifactParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.MODIFY_CODE,
      "Replace one exact, unique text block in an existing project source/config file. Read the file first; paths outside the repository and ambiguous replacements are rejected.",
      "Modify code",
      modifyCodeParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.APPLY_PATCH,
      "Atomically apply up to 20 unique exact text replacements across distinct existing project source/config files. First read every target; if any oldText is absent or ambiguous, no files are modified. Create/delete/rename and arbitrary unified-diff syntax are not supported.",
      "Apply patch",
      applyPatchParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.RUN_TESTS,
      "Run one server-approved npm verification script (currently test or build). Arbitrary shell commands and arguments are rejected.",
      "Run tests",
      runTestsParameters,
      runtime,
      context,
    ),
    /**
     * 工具顺序固定，所有工具串行，避免同一个进程内的计划状态被并发请求交错修改。
     */
    createRuntimeTool(
      ToolName.CREATE_PLAN,
      "Create a dependency-aware execution plan. This only proposes a plan; server validation owns plan state.",
      "Create plan",
      createPlanParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.GET_PLAN,
      "Read the current server-owned plan state and its evidence.",
      "Get plan",
      getPlanParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.UPDATE_PLAN_STEP,
      "Request a plan step transition. Completion is accepted only after server-side evidence verification.",
      "Update plan step",
      updatePlanStepParameters,
      runtime,
      context,
    ),
    createRuntimeTool(
      ToolName.REVISE_PLAN,
      "Request an atomic revision of uncompleted plan steps using an expected plan version.",
      "Revise plan",
      revisePlanParameters,
      runtime,
      context,
    ),
  ];
}

/**
 * planStepId 只用于 Pi Hook 将真实执行绑定到计划步骤，不能泄露给 Runtime 参数 Schema。
 * Adapter 不解释其它业务字段，仍是到唯一 Runtime.execute 的薄转发层。
 */
function stripPlanStepId(parameters: unknown): unknown {
  if (parameters === null || typeof parameters !== "object" || Array.isArray(parameters)) return parameters;
  const { planStepId: _planStepId, ...runtimeArguments } = parameters as Record<string, unknown>;
  return runtimeArguments;
}

function createRuntimeTool<TParameters extends typeof listFilesParameters>(
  toolName: typeof ToolName.LIST_FILES,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof searchCodeParameters>(
  toolName: typeof ToolName.SEARCH_CODE,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof readFileParameters>(
  toolName: typeof ToolName.READ_FILE,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof writeArtifactParameters>(
  toolName: typeof ToolName.WRITE_ARTIFACT,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof modifyCodeParameters>(
  toolName: typeof ToolName.MODIFY_CODE,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof applyPatchParameters>(
  toolName: typeof ToolName.APPLY_PATCH,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof runTestsParameters>(
  toolName: typeof ToolName.RUN_TESTS,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof createPlanParameters | typeof getPlanParameters | typeof updatePlanStepParameters | typeof revisePlanParameters>(
  toolName: typeof ToolName.CREATE_PLAN | typeof ToolName.GET_PLAN | typeof ToolName.UPDATE_PLAN_STEP | typeof ToolName.REVISE_PLAN,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails>;
function createRuntimeTool<TParameters extends typeof listFilesParameters | typeof searchCodeParameters | typeof readFileParameters | typeof writeArtifactParameters | typeof modifyCodeParameters | typeof applyPatchParameters | typeof runTestsParameters | typeof createPlanParameters | typeof getPlanParameters | typeof updatePlanStepParameters | typeof revisePlanParameters>(
  toolName: ToolName,
  description: string,
  label: string,
  parameters: TParameters,
  runtime: RuntimeExecutor,
  context: RunExecutionContext,
): AgentTool<TParameters, RuntimeToolDetails> {
  return {
    name: toolName,
    label,
    description,
    parameters,
    executionMode: "sequential",
    async execute(toolCallId, modelArguments, signal): Promise<AgentToolResult<RuntimeToolDetails>> {
      const runtimeArguments = stripPlanStepId(modelArguments);
      const result = await runtime.execute({
        toolCallId,
        toolName,
        modelArguments: runtimeArguments,
        context,
        signal,
      } satisfies ToolExecutionRequest);

      if (!result.success) {
        throw new RuntimeToolFailure(result);
      }

      return {
        content: [{ type: "text", text: result.content }],
        details: {
          code: result.code,
          ...(result.artifactPath === undefined ? {} : { artifactPath: result.artifactPath }),
          ...(result.evidenceDraft === undefined ? {} : { evidenceDraft: result.evidenceDraft }),
          ...(result.verification === undefined ? {} : { verification: result.verification }),
        },
      };
    },
  };
}

export type ListFilesToolParameters = Static<typeof listFilesParameters>;
export type SearchCodeToolParameters = Static<typeof searchCodeParameters>;
export type ReadFileToolParameters = Static<typeof readFileParameters>;
export type WriteArtifactToolParameters = Static<typeof writeArtifactParameters>;
