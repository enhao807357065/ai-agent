import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, relative, resolve, sep } from "node:path";
import { Type, type Static, type TSchema } from "typebox";
import { Check } from "typebox/value";
import type { RunExecutionContext } from "./execution-context.js";

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
} as const;

export type ToolName = (typeof ToolName)[keyof typeof ToolName];

export const ToolResultCode = {
  OK: "ok",
  TOOL_NOT_FOUND: "tool_not_found",
  INVALID_ARGUMENT: "invalid_argument",
  PATH_REJECTED: "path_rejected",
  ARTIFACT_PATH_REJECTED: "artifact_path_rejected",
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

type ListFilesParameters = Static<typeof listFilesParameters>;
type SearchCodeParameters = Static<typeof searchCodeParameters>;
type ReadFileParameters = Static<typeof readFileParameters>;
type WriteArtifactParameters = Static<typeof writeArtifactParameters>;

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

type ToolHandler = (
  parameters: unknown,
  context: RunExecutionContext,
  signal: AbortSignal | undefined,
) => Promise<HandlerOutput>;

interface HandlerOutput {
  readonly content: string;
  readonly artifactPath?: string;
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
export class ToolRuntime {
  readonly #tools: ReadonlyMap<string, RegisteredTool>;
  readonly #handlerExecutionCounts = new Map<string, number>();

  constructor() {
    this.#tools = new Map<string, RegisteredTool>([
      [ToolName.LIST_FILES, { schema: listFilesParameters, handler: listFiles }],
      [ToolName.SEARCH_CODE, { schema: searchCodeParameters, handler: searchCode }],
      [ToolName.READ_FILE, { schema: readFileParameters, handler: readSourceFile }],
      [ToolName.WRITE_ARTIFACT, { schema: writeArtifactParameters, handler: writeAnalysisArtifact }],
    ]);
  }

  getHandlerExecutionCount(toolName: string): number {
    return this.#handlerExecutionCounts.get(toolName) ?? 0;
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
      return successResult(request.toolCallId, output.content, output.artifactPath);
    } catch (error) {
      if (error instanceof KnownToolError) {
        return failureResult(request.toolCallId, error.code, error.message);
      }
      return failureResult(
        request.toolCallId,
        ToolResultCode.EXECUTION_FAILED,
        "Tool execution failed.",
      );
    }
  }
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
    default:
      throw new KnownToolError(ToolResultCode.TOOL_NOT_FOUND, "The requested tool is not registered.");
  }
}

function successResult(
  toolCallId: string,
  content: string,
  artifactPath?: string,
): ControlledToolResult {
  return Object.freeze({
    toolCallId,
    success: true,
    code: ToolResultCode.OK,
    content,
    ...(artifactPath === undefined ? {} : { artifactPath }),
  });
}

function failureResult(
  toolCallId: string,
  code: FailureToolResultCode,
  content: string,
): ControlledToolResult {
  return Object.freeze({
    toolCallId,
    success: false,
    code,
    content,
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
