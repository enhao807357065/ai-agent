import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolResultCode, ToolRuntime } from "../src/runtime/tool-runtime.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true })));
});

async function createFixture() {
  const base = await mkdtemp(join(tmpdir(), "week03-runtime-"));
  temporaryDirectories.push(base);
  const repositoryRoot = join(base, "repository");
  const artifactsRoot = join(base, "artifacts");
  await mkdir(join(repositoryRoot, "src"), { recursive: true });
  await mkdir(join(repositoryRoot, "node_modules", "ignored"), { recursive: true });
  await mkdir(join(repositoryRoot, ".git", "ignored"), { recursive: true });
  await writeFile(join(repositoryRoot, "src", "auth.ts"), "export const login = 'login';\n", "utf8");
  await writeFile(join(repositoryRoot, "README.md"), "login flow\n", "utf8");
  await writeFile(join(repositoryRoot, "node_modules", "ignored", "package.ts"), "login", "utf8");

  return {
    repositoryRoot,
    artifactsRoot,
    context: createRunExecutionContext({ projectRoot: base, repositoryRoot, artifactsRoot }),
  };
}

describe("ToolRuntime", () => {
  it("keeps tool_call_id and lists repository files without dependency or VCS directories", async () => {
    const fixture = await createFixture();
    const result = await new ToolRuntime().execute({
      toolCallId: "call-list-1",
      toolName: ToolName.LIST_FILES,
      modelArguments: {},
      context: fixture.context,
    });

    expect(result).toMatchObject({ toolCallId: "call-list-1", success: true, code: ToolResultCode.OK });
    expect(result.content).toBe("README.md\nsrc/auth.ts");
  });

  it("searches only supported text files in stable path order", async () => {
    const fixture = await createFixture();
    const result = await new ToolRuntime().execute({
      toolCallId: "call-search-1",
      toolName: ToolName.SEARCH_CODE,
      modelArguments: { query: "login" },
      context: fixture.context,
    });

    expect(result).toMatchObject({ toolCallId: "call-search-1", success: true });
    expect(result.content).toBe("README.md:1:login flow\nsrc/auth.ts:1:export const login = 'login';");
  });

  it("rejects invalid and outside paths before the handler runs", async () => {
    const fixture = await createFixture();
    const runtime = new ToolRuntime();
    const invalid = await runtime.execute({
      toolCallId: "call-invalid",
      toolName: ToolName.READ_FILE,
      modelArguments: { unexpected: true },
      context: fixture.context,
    });
    const outside = await runtime.execute({
      toolCallId: "call-outside",
      toolName: ToolName.READ_FILE,
      modelArguments: { path: "../outside.txt" },
      context: fixture.context,
    });

    expect(invalid).toMatchObject({ success: false, code: ToolResultCode.INVALID_ARGUMENT });
    expect(outside).toMatchObject({ success: false, code: ToolResultCode.PATH_REJECTED });
    expect(runtime.getHandlerExecutionCount(ToolName.READ_FILE)).toBe(0);
  });

  it("writes only inside the artifact root and returns a controlled artifact path", async () => {
    const fixture = await createFixture();
    const runtime = new ToolRuntime();
    const result = await runtime.execute({
      toolCallId: "call-write-1",
      toolName: ToolName.WRITE_ARTIFACT,
      modelArguments: { path: "reports/login-flow.md", content: "verified flow" },
      context: fixture.context,
    });
    const rejected = await runtime.execute({
      toolCallId: "call-write-2",
      toolName: ToolName.WRITE_ARTIFACT,
      modelArguments: { path: "../repository/escape.md", content: "must not write" },
      context: fixture.context,
    });

    expect(result).toMatchObject({
      toolCallId: "call-write-1",
      success: true,
      code: ToolResultCode.OK,
      artifactPath: "reports/login-flow.md",
    });
    await expect(readFile(join(fixture.artifactsRoot, "reports", "login-flow.md"), "utf8")).resolves.toBe("verified flow");
    expect(rejected).toMatchObject({ success: false, code: ToolResultCode.ARTIFACT_PATH_REJECTED });
  });

  it("normalizes cancelled and unknown calls without invoking a handler", async () => {
    const fixture = await createFixture();
    const runtime = new ToolRuntime();
    const controller = new AbortController();
    controller.abort();
    const cancelled = await runtime.execute({
      toolCallId: "call-cancelled",
      toolName: ToolName.LIST_FILES,
      modelArguments: {},
      context: fixture.context,
      signal: controller.signal,
    });
    const unknown = await runtime.execute({
      toolCallId: "call-unknown",
      toolName: "not_registered",
      modelArguments: {},
      context: fixture.context,
    });

    expect(cancelled).toMatchObject({ toolCallId: "call-cancelled", success: false, code: ToolResultCode.CANCELLED });
    expect(unknown).toMatchObject({ toolCallId: "call-unknown", success: false, code: ToolResultCode.TOOL_NOT_FOUND });
    expect(runtime.getHandlerExecutionCount(ToolName.LIST_FILES)).toBe(0);
  });
});
