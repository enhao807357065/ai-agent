import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { PlanStore, StepKind, StepKindVerifier } from "../src/planning/plan-store.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { CodeVersionTracker } from "../src/planning/code-version.js";
import { ToolName, ToolRuntime } from "../src/runtime/tool-runtime.js";

const temporary: string[] = [];
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))); });

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "week03-harness-verification-"));
  temporary.push(root);
  const repositoryRoot = join(root, "repo");
  const artifactsRoot = join(root, "artifacts");
  await mkdir(repositoryRoot);
  await writeFile(join(repositoryRoot, "package.json"), JSON.stringify({ scripts: { test: "node -e \"process.exit(0)\"" } }), "utf8");
  await writeFile(join(repositoryRoot, "subject.ts"), "export const enabled = false;\n", "utf8");
  return {
    repositoryRoot,
    context: createRunExecutionContext({ projectRoot: root, repositoryRoot, artifactsRoot, taskArtifactPath: "report.md" }),
  };
}

function verificationStore() {
  const version = new CodeVersionTracker();
  const store = new PlanStore(() => version.current(), new StepKindVerifier());
  store.create("repair", [
    { id: "implement", title: "implement", kind: StepKind.IMPLEMENTATION },
    { id: "verify", title: "verify", kind: StepKind.VERIFICATION, dependsOn: ["implement"] },
  ]);
  store.start("repair", "implement");
  return { version, store };
}

describe("verification evidence is bound to actual tested content", () => {
  it("invalidates all completed verification evidence when a later source change changes the tested content", async () => {
    const { context } = await fixture();
    const { version, store } = verificationStore();
    const runtime = new ToolRuntime({ planStore: store, codeVersionTracker: version });

    const change = await runtime.execute({ toolCallId: "change-1", toolName: ToolName.MODIFY_CODE, modelArguments: { path: "subject.ts", oldText: "false", newText: "true" }, context });
    expect(change.success).toBe(true);
    store.recordToolEvidence("repair", "implement", { toolCallId: "change-1", kind: "change", summary: "changed subject" });
    store.complete("repair", "implement", ["tool:change-1:1"]);
    store.start("repair", "verify");

    const test = await runtime.execute({ toolCallId: "test-1", toolName: ToolName.RUN_TESTS, modelArguments: { script: "test" }, context });
    expect(test.success).toBe(true);
    store.recordToolEvidence("repair", "verify", { toolCallId: "test-1", kind: "test", summary: "test passed" });
    store.recordVerificationOutcome("repair", { toolCallId: "test-1", scope: test.verification!.scope, testedDigest: test.verification!.testedDigest, passed: true });
    store.complete("repair", "verify", ["tool:test-1:2"]);
    expect(store.completionIssues("repair")).toEqual([]);

    const laterChange = await runtime.execute({ toolCallId: "change-2", toolName: ToolName.MODIFY_CODE, modelArguments: { path: "subject.ts", oldText: "true", newText: "false" }, context });
    expect(laterChange.success).toBe(true);

    expect(store.completionIssues("repair")).toEqual([
      expect.stringContaining("test-1"),
    ]);
  });

  it("uses the latest result for the same verification scope, so a later failure overrides an earlier pass", async () => {
    const { store } = verificationStore();
    store.recordVerificationOutcome("repair", { toolCallId: "test-pass", scope: "npm:test", testedDigest: "same-content", passed: true });
    store.recordVerificationOutcome("repair", { toolCallId: "test-fail", scope: "npm:test", testedDigest: "same-content", passed: false, failureCode: "test_failed" });

    expect(store.completionIssues("repair")).toEqual([
      expect.stringContaining("test-fail"),
    ]);
  });
});
