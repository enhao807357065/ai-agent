import type { AgentTool, AgentToolResult } from "@earendil-works/pi-agent-core";
import { Type, type Static } from "typebox";
import type { RunExecutionContext } from "../runtime/execution-context.js";
import {
  ToolName,
  type ControlledToolResult,
  type ToolExecutionRequest,
  type ToolRuntime,
} from "../runtime/tool-runtime.js";

const listFilesParameters = Type.Object(
  {
    startPath: Type.Optional(Type.String({ minLength: 1 })),
  },
  { additionalProperties: false },
);

const searchCodeParameters = Type.Object(
  {
    query: Type.String({ minLength: 1 }),
    startPath: Type.Optional(Type.String({ minLength: 1 })),
  },
  { additionalProperties: false },
);

const readFileParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
  },
  { additionalProperties: false },
);

const writeArtifactParameters = Type.Object(
  {
    path: Type.String({ minLength: 1 }),
    content: Type.String({ minLength: 1 }),
  },
  { additionalProperties: false },
);

type RuntimeExecutor = Pick<ToolRuntime, "execute">;

export interface RuntimeToolDetails {
  readonly code: ControlledToolResult["code"];
  readonly artifactPath?: string;
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
  ];
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
function createRuntimeTool<TParameters extends typeof listFilesParameters | typeof searchCodeParameters | typeof readFileParameters | typeof writeArtifactParameters>(
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
      const result = await runtime.execute({
        toolCallId,
        toolName,
        modelArguments,
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
          ...(result.artifactPath === undefined
            ? {}
            : { artifactPath: result.artifactPath }),
        },
      };
    },
  };
}

export type ListFilesToolParameters = Static<typeof listFilesParameters>;
export type SearchCodeToolParameters = Static<typeof searchCodeParameters>;
export type ReadFileToolParameters = Static<typeof readFileParameters>;
export type WriteArtifactToolParameters = Static<typeof writeArtifactParameters>;
