import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { CodeVersionTracker } from "../src/planning/code-version.js";
import { PlanStore, StepKind, StepKindVerifier } from "../src/planning/plan-store.js";
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

  it("applies multi-file replacements atomically, produces one change evidence draft, and advances one version", async () => {
    const fixture = await createFixture();
    await writeFile(join(fixture.repositoryRoot, "README.md"), "login documentation\n", "utf8");
    const tracker = new CodeVersionTracker();
    const runtime = new ToolRuntime({ codeVersionTracker: tracker });

    const result = await runtime.execute({
      toolCallId: "call-patch-ok",
      toolName: ToolName.APPLY_PATCH,
      modelArguments: {
        patches: [
          { path: "src/auth.ts", oldText: "'login'", newText: "'login-with-expiry'" },
          { path: "README.md", oldText: "login documentation", newText: "expiry documentation" },
        ],
      },
      context: fixture.context,
    });

    expect(result).toMatchObject({ success: true, code: ToolResultCode.OK, evidenceDraft: { kind: "change" } });
    expect(result.content).toContain("src/auth.ts, README.md");
    expect(tracker.current()).toBe("run-code-1");
    await expect(readFile(join(fixture.repositoryRoot, "src", "auth.ts"), "utf8")).resolves.toContain("login-with-expiry");
    await expect(readFile(join(fixture.repositoryRoot, "README.md"), "utf8")).resolves.toContain("expiry documentation");
  });

  it("rejects an invalid patch without changing any target file or code version", async () => {
    const fixture = await createFixture();
    const tracker = new CodeVersionTracker();
    const runtime = new ToolRuntime({ codeVersionTracker: tracker });

    const result = await runtime.execute({
      toolCallId: "call-patch-rejected",
      toolName: ToolName.APPLY_PATCH,
      modelArguments: {
        patches: [
          { path: "src/auth.ts", oldText: "'login'", newText: "'changed'" },
          { path: "README.md", oldText: "does not exist", newText: "x" },
        ],
      },
      context: fixture.context,
    });

    expect(result).toMatchObject({ success: false, code: ToolResultCode.INVALID_ARGUMENT });
    expect(tracker.current()).toBe("run-code-0");
    await expect(readFile(join(fixture.repositoryRoot, "src", "auth.ts"), "utf8")).resolves.toContain("'login'");
  });

  it("modifies one unique source block, produces change evidence, and rejects ambiguous/outside edits", async () => {
    const fixture = await createFixture();
    const runtime = new ToolRuntime();
    const changed = await runtime.execute({
      toolCallId: "call-modify-1",
      toolName: ToolName.MODIFY_CODE,
      modelArguments: { path: "src/auth.ts", oldText: "'login'", newText: "'login-with-expiry'" },
      context: fixture.context,
    });
    const ambiguous = await runtime.execute({
      toolCallId: "call-modify-2",
      toolName: ToolName.MODIFY_CODE,
      modelArguments: { path: "src/auth.ts", oldText: "login", newText: "login" },
      context: fixture.context,
    });
    const outside = await runtime.execute({
      toolCallId: "call-modify-3",
      toolName: ToolName.MODIFY_CODE,
      modelArguments: { path: "../outside.ts", oldText: "x", newText: "y" },
      context: fixture.context,
    });

    expect(changed).toMatchObject({ success: true, code: ToolResultCode.OK, artifactPath: "src/auth.ts", evidenceDraft: { kind: "change", summary: "src/auth.ts" } });
    await expect(readFile(join(fixture.repositoryRoot, "src", "auth.ts"), "utf8")).resolves.toContain("login-with-expiry");
    expect(ambiguous).toMatchObject({ success: false, code: ToolResultCode.INVALID_ARGUMENT });
    expect(outside).toMatchObject({ success: false, code: ToolResultCode.PATH_REJECTED });
  });

  it("advances the server code version only after a successful modify_code", async () => {
    const fixture = await createFixture();
    const tracker = new CodeVersionTracker();
    const store = new PlanStore(() => tracker.current(), new StepKindVerifier());
    store.create("repair", [{ id: "implement", title: "change", kind: StepKind.IMPLEMENTATION }]);
    store.start("repair", "implement");
    const runtime = new ToolRuntime({ planStore: store, codeVersionTracker: tracker });

    expect(tracker.current()).toBe("run-code-0");
    const changed = await runtime.execute({
      toolCallId: "call-versioned-change",
      toolName: ToolName.MODIFY_CODE,
      modelArguments: { path: "src/auth.ts", oldText: "'login'", newText: "'fixed'" },
      context: fixture.context,
    });
    expect(changed.success).toBe(true);
    expect(tracker.current()).toBe("run-code-1");
    const rejected = await runtime.execute({
      toolCallId: "call-rejected-change",
      toolName: ToolName.MODIFY_CODE,
      modelArguments: { path: "src/auth.ts", oldText: "missing", newText: "x" },
      context: fixture.context,
    });
    expect(rejected.success).toBe(false);
    expect(tracker.current()).toBe("run-code-1");
  });

  it("runs only allowlisted npm scripts and produces test evidence after success", async () => {
    const fixture = await createFixture();
    await writeFile(join(fixture.repositoryRoot, "package.json"), JSON.stringify({ scripts: { build: "node -e 'process.exit(0)'" } }), "utf8");
    const runtime = new ToolRuntime({ allowedTestScripts: ["build"] });
    const success = await runtime.execute({ toolCallId: "call-test-1", toolName: ToolName.RUN_TESTS, modelArguments: { script: "build" }, context: fixture.context });
    const rejected = await runtime.execute({ toolCallId: "call-test-2", toolName: ToolName.RUN_TESTS, modelArguments: { script: "build && rm -rf /" }, context: fixture.context });

    expect(success).toMatchObject({ success: true, evidenceDraft: { kind: "test", summary: "npm run build: exitCode=0" } });
    expect(rejected).toMatchObject({ success: false, code: ToolResultCode.TEST_COMMAND_REJECTED });
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
