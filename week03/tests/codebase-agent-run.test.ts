import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { Context } from "@earendil-works/pi-ai";
import { codebaseAgentModel } from "../src/model/gateway-models.js";
import {
  AssemblyStopReason,
  runCodebaseAgent,
} from "../src/agent/codebase-agent-run.js";
import { StopReason } from "../src/agent/run-stop-center.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolRuntime } from "../src/runtime/tool-runtime.js";
import { createScriptedStream, type ScriptedResponse } from "./scripted-stream.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

async function createFixture() {
  const root = await mkdtemp(join(tmpdir(), "week03-agent-loop-"));
  temporaryDirectories.push(root);
  const repositoryRoot = join(root, "repository");
  const artifactsRoot = join(root, "artifacts");
  await mkdir(join(repositoryRoot, "src", "routes"), { recursive: true });
  await mkdir(join(repositoryRoot, "src", "services"), { recursive: true });
  await mkdir(join(repositoryRoot, "src", "repositories"), { recursive: true });
  await writeFile(join(repositoryRoot, "src", "routes", "login.ts"), "export const loginRoute = () => authenticate();\n", "utf8");
  await writeFile(join(repositoryRoot, "src", "services", "auth-service.ts"), "export const authenticate = () => loadUser();\n", "utf8");
  await writeFile(join(repositoryRoot, "src", "repositories", "user-repository.ts"), "export const loadUser = () => null;\n", "utf8");
  return {
    repositoryRoot,
    artifactsRoot,
    context: createRunExecutionContext({
      projectRoot: root,
      repositoryRoot,
      artifactsRoot,
      taskArtifactPath: "login-flow.md",
    }),
  };
}

function run(
  responses: readonly ScriptedResponse[],
  context: Awaited<ReturnType<typeof createFixture>>["context"],
  overrides: Partial<Parameters<typeof runCodebaseAgent>[0]> = {},
  observedContexts: Context[] = [],
) {
  return runCodebaseAgent({
    model: codebaseAgentModel,
    streamFn: createScriptedStream(responses, observedContexts),
    executionContext: context,
    ...overrides,
  });
}

describe("Pi native codebase-agent assembly", () => {
  it("writes the first tool result back to the second model context with the same toolCallId", async () => {
    const fixture = await createFixture();
    const observed: Context[] = [];
    const result = await run([
      { kind: "tool", id: "search-1", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
      { kind: "final", text: "done" },
    ], fixture.context, { maxTurns: 2 }, observed);

    expect(observed).toHaveLength(2);
    expect(observed[1].messages).toContainEqual(expect.objectContaining({
      role: "toolResult", toolCallId: "search-1", toolName: ToolName.SEARCH_CODE,
    }));
    expect(result.trace).toContainEqual(expect.objectContaining({
      type: "tool_started", toolCallId: "search-1",
    }));
  });

  it("rejects an early final, injects FINAL_REJECTED, then accepts the completed delivery", async () => {
    const fixture = await createFixture();
    const observed: Context[] = [];
    const result = await run([
      { kind: "final", text: "premature" },
      { kind: "tool", id: "read-1", name: ToolName.READ_FILE, arguments: { path: "src/routes/login.ts" } },
      { kind: "tool", id: "read-2", name: ToolName.READ_FILE, arguments: { path: "src/services/auth-service.ts" } },
      { kind: "tool", id: "read-3", name: ToolName.READ_FILE, arguments: { path: "src/repositories/user-repository.ts" } },
      { kind: "tool", id: "write-1", name: ToolName.WRITE_ARTIFACT, arguments: { path: "login-flow.md", content: "# Login\n- src/routes/login.ts" } },
      { kind: "final", text: "complete" },
    ], fixture.context, { maxTurns: 8 }, observed);

    expect(observed[1].messages).toContainEqual(expect.objectContaining({
      role: "user", content: expect.stringContaining("FINAL_REJECTED"),
    }));
    expect(result.state.stopReason).toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
    expect(result.state.stopCenter.readSourceFiles).toHaveLength(3);
    expect(result.state.stopCenter.writtenArtifacts).toContain("login-flow.md");
  });

  it("blocks a third repeated action before the Runtime handler and returns an error tool result", async () => {
    const fixture = await createFixture();
    const runtime = new ToolRuntime();
    const result = await run([
      { kind: "tool", id: "search-1", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
      { kind: "tool", id: "search-2", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
      { kind: "tool", id: "search-3", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
    ], fixture.context, { runtime, maxTurns: 8 });

    expect(runtime.getHandlerExecutionCount(ToolName.SEARCH_CODE)).toBe(2);
    expect(result.messages.at(-1)).toMatchObject({ role: "toolResult", toolCallId: "search-3", isError: true });
    expect(result.state.stopReason).toBe(StopReason.REPEATED_ACTION);
  });

  it("stops after exactly the configured maximum turn count without a fourth model request", async () => {
    const fixture = await createFixture();
    const observed: Context[] = [];
    await run([
      { kind: "tool", id: "a", name: ToolName.SEARCH_CODE, arguments: { query: "route" } },
      { kind: "tool", id: "b", name: ToolName.SEARCH_CODE, arguments: { query: "service" } },
      { kind: "tool", id: "c", name: ToolName.SEARCH_CODE, arguments: { query: "repository" } },
      { kind: "tool", id: "d", name: ToolName.SEARCH_CODE, arguments: { query: "unused" } },
    ], fixture.context, { maxTurns: 3 }, observed).then((result) => {
      expect(result.state.stopReason).toBe(StopReason.MAX_TURNS_REACHED);
    });
    expect(observed).toHaveLength(3);
  });

  it("propagates cancellation from a slow stream and does not begin tool execution", async () => {
    const fixture = await createFixture();
    const controller = new AbortController();
    const promise = run([{ kind: "aborted" }], fixture.context, { signal: controller.signal });
    controller.abort();
    const result = await promise;

    expect(result.state.stopReason).toBe(AssemblyStopReason.CANCELLED);
    expect(result.trace.some((entry) => entry.type === "tool_started")).toBe(false);
  });

  it("completes the real entry-service-repository login understanding flow in an isolated artifact directory", async () => {
    const fixture = await createFixture();
    const result = await run([
      { kind: "tool", id: "search-login", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
      { kind: "tool", id: "read-route", name: ToolName.READ_FILE, arguments: { path: "src/routes/login.ts" } },
      { kind: "tool", id: "read-service", name: ToolName.READ_FILE, arguments: { path: "src/services/auth-service.ts" } },
      { kind: "tool", id: "read-repository", name: ToolName.READ_FILE, arguments: { path: "src/repositories/user-repository.ts" } },
      { kind: "tool", id: "write-flow", name: ToolName.WRITE_ARTIFACT, arguments: {
        path: "login-flow.md",
        content: "# Login flow\n1. `src/routes/login.ts` calls the service.\n2. `src/services/auth-service.ts` calls the repository.\n3. `src/repositories/user-repository.ts` loads the user.\n",
      } },
      { kind: "final", text: "Used src/routes/login.ts, src/services/auth-service.ts, and src/repositories/user-repository.ts." },
    ], fixture.context, { maxTurns: 8 });

    const artifact = await readFile(join(fixture.artifactsRoot, "login-flow.md"), "utf8");
    expect(artifact).toContain("src/routes/login.ts");
    expect(artifact).toContain("src/services/auth-service.ts");
    expect(artifact).toContain("src/repositories/user-repository.ts");
    expect(result.trace.some((entry) => entry.type === "tool_started")).toBe(true);
    expect(result.trace.some((entry) => entry.type === "tool_finished")).toBe(true);
    expect(result.state.stopReason).toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
  });
});
