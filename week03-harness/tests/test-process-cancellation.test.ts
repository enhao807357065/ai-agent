import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolResultCode, ToolRuntime } from "../src/runtime/tool-runtime.js";

const temporary: string[] = [];
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))); });

describe("test-process cancellation", () => {
  it("waits for the spawned npm test process to close, then returns the controlled cancellation result", async () => {
    const root = await mkdtemp(join(tmpdir(), "week03-harness-cancel-"));
    temporary.push(root);
    const repositoryRoot = join(root, "repo");
    await mkdir(repositoryRoot);
    await writeFile(join(repositoryRoot, "package.json"), JSON.stringify({ scripts: { test: "node -e \"setTimeout(() => {}, 10000)\"" } }), "utf8");
    const context = createRunExecutionContext({ projectRoot: root, repositoryRoot, artifactsRoot: join(root, "artifacts"), taskArtifactPath: "report.md" });
    const controller = new AbortController();
    const runtime = new ToolRuntime({ testTimeoutMs: 15_000 });

    const running = runtime.execute({ toolCallId: "slow-test", toolName: ToolName.RUN_TESTS, modelArguments: { script: "test" }, context, signal: controller.signal });
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));
    controller.abort();

    await expect(running).resolves.toMatchObject({ success: false, code: ToolResultCode.CANCELLED });
    expect(runtime.getHandlerExecutionCount(ToolName.RUN_TESTS)).toBe(1);
  });
});
