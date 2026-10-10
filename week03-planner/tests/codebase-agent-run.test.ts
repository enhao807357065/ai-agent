import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { Context } from "@earendil-works/pi-ai";
import { codebaseAgentModel } from "../src/model/gateway-models.js";
import {
  AssemblyStopReason,
  codebaseAgentSystemPrompt,
  runCodebaseAgent,
} from "../src/agent/codebase-agent-run.js";
import { StopReason } from "../src/agent/run-stop-center.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { PlanStore, StepKind, StepKindVerifier, StepStatus } from "../src/planning/plan-store.js";
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
  it("exposes task-agnostic dynamic planning, evidence, revision and final-report rules", () => {
    expect(codebaseAgentSystemPrompt).toContain("任务目标只来自用户任务和当前 [PLAN_STATE]");
    expect(codebaseAgentSystemPrompt).toContain("dependsOn 和完成标准");
    expect(codebaseAgentSystemPrompt).toContain("planStepId");
    expect(codebaseAgentSystemPrompt).toContain("revise_plan");
    expect(codebaseAgentSystemPrompt).toContain("真实 evidenceIds");
    expect(codebaseAgentSystemPrompt).toContain("实际验证命令与结果");
    expect(codebaseAgentSystemPrompt).not.toContain("登录逻辑");
    expect(codebaseAgentSystemPrompt).not.toContain("login-flow.md");
  });

  it("continues from checkpoint messages without injecting a duplicate task prompt", async () => {
    const fixture = await createFixture();
    const observed: Context[] = [];
    await run([{ kind: "final", text: "done" }], fixture.context, {
      maxTurns: 1,
      resumeMessages: [{ role: "user", content: "persisted task", timestamp: 1 }],
    }, observed);
    expect(observed[0].messages).toContainEqual(expect.objectContaining({ role: "user", content: "persisted task" }));
    expect(observed[0].messages.filter((message) => message.role === "user" && message.content === "排查并修复 login_demo 的 access token 会话过期边界问题。先确认 token 签发、解析和鉴权链路；定义 now === expiresAt 时必须失效的边界；完成实现与边界验证，并报告未验证项。")).toHaveLength(0);
  });

  it("terminates the real Pi tool batch on approval-required, so no later model turn can bypass review", async () => {
    const fixture = await createFixture();
    await writeFile(join(fixture.repositoryRoot, "src", "token.ts"), "export const expired = false;\n");
    const approval = new (await import("../src/approval/source-write-approval.js")).SourceWriteApprovalGate();
    const hook = new (await import("../src/approval/source-write-approval-hook.js")).SourceWriteApprovalHook(approval, fixture.context.repositoryRoot);
    const result = await run([{ kind: "tool", id: "write-pending", name: ToolName.MODIFY_CODE, arguments: { path: "src/token.ts", oldText: "false", newText: "true" } }], fixture.context, {
      sourceWriteApproval: hook,
    });
    expect(result.state.stopReason).toBe("waiting_for_approval");
    expect(result.runtime.getHandlerExecutionCount("modify_code")).toBe(0);
  });

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

  it("injects one refreshed Plan snapshot, requires planStepId for a governed write, strips it before Runtime, and records server-stamped evidence", async () => {
    const fixture = await createFixture();
    const observed: Context[] = [];
    const store = new PlanStore("git-current");
    store.create("plan-1", [{ id: "deliver", title: "write delivery" }]);
    store.start("plan-1", "deliver");

    const result = await run([
      { kind: "tool", id: "write-1", name: ToolName.WRITE_ARTIFACT, arguments: { path: "login-flow.md", content: "# delivery", planStepId: "deliver" } },
      { kind: "final", text: "done" },
    ], fixture.context, { planStore: store, planId: "plan-1", maxTurns: 3 }, observed);

    const evidence = store.get("plan-1").evidence;
    expect(evidence).toContainEqual(expect.objectContaining({ stepId: "deliver", toolCallId: "write-1", codeVersion: "git-current", kind: "change" }));
    expect(store.get("plan-1").steps[0].status).toBe(StepStatus.RUNNING);
    expect(observed.every((context) => context.messages.filter((message) => message.role === "user" && typeof message.content === "string" && message.content.startsWith("[PLAN_STATE]")).length === 1)).toBe(true);
    expect(result.state.stopReason).not.toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
  });

  it("emits plan snapshot, tool lifecycle, evidence, and stop-decision logs without model/tool content", async () => {
    const fixture = await createFixture();
    const store = new PlanStore("git-current");
    store.create("plan-logs", [{ id: "deliver", title: "write delivery" }]);
    store.start("plan-logs", "deliver");
    const logs: string[] = [];

    await run([
      { kind: "tool", id: "write-log", name: ToolName.WRITE_ARTIFACT, arguments: { path: "login-flow.md", content: "# delivery", planStepId: "deliver" } },
      { kind: "final", text: "done" },
    ], fixture.context, { planStore: store, planId: "plan-logs", maxTurns: 3, onLog: (message) => logs.push(message) });

    expect(logs).toEqual(expect.arrayContaining([
      expect.stringContaining("run.started"),
      expect.stringContaining("plan.snapshot.initial planId=plan-logs version=1"),
      expect.stringContaining("turn.started number=1"),
      expect.stringContaining("tool.requested name=write_artifact callId=write-log"),
      expect.stringContaining("plan.step.bound stepId=deliver kind=implementation status=running"),
      expect.stringContaining("plan.evidence.recorded"),
      expect.stringContaining("plan.snapshot.refresh removed=1"),
      expect.stringContaining("loop.continue reason=FINAL_REJECTED"),
      expect.stringContaining("run.finished"),
    ]));
    expect(logs.join("\n")).not.toContain("# delivery");
  });


  it("blocks a planned write with no planStepId before it reaches the Runtime", async () => {
    const fixture = await createFixture();
    const store = new PlanStore("v1");
    store.create("plan-1", [{ id: "deliver", title: "write delivery" }]);
    store.start("plan-1", "deliver");
    const runtime = new ToolRuntime();
    const result = await run([
      { kind: "tool", id: "write-no-step", name: ToolName.WRITE_ARTIFACT, arguments: { path: "login-flow.md", content: "no" } },
    ], fixture.context, { planStore: store, planId: "plan-1", runtime, maxTurns: 3 });

    expect(runtime.getHandlerExecutionCount(ToolName.WRITE_ARTIFACT)).toBe(0);
    expect(result.messages).toContainEqual(expect.objectContaining({ role: "toolResult", toolCallId: "write-no-step", isError: true }));
  });

  it("executes modify_code and run_tests through the governed repair DAG, writes trusted evidence, and completes the fix contract", async () => {
    const fixture = await createFixture();
    await writeFile(join(fixture.repositoryRoot, "package.json"), JSON.stringify({
      scripts: { test: "node -e \"process.exit(0)\"", build: "node -e \"process.exit(0)\"" },
    }), "utf8");
    const tokenPath = join(fixture.repositoryRoot, "src", "token.ts");
    await writeFile(tokenPath, "export const expiry = false;\n", "utf8");

    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("repair", [
      { id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION },
      { id: "design", title: "design", kind: StepKind.DESIGN, dependsOn: ["diagnose"] },
      { id: "implement", title: "implement", kind: StepKind.IMPLEMENTATION, dependsOn: ["design"] },
      { id: "verify", title: "verify", kind: StepKind.VERIFICATION, dependsOn: ["implement"] },
    ]);
    const runtime = new ToolRuntime({ planStore: store });
    const result = await run([
      { kind: "tool", id: "start-diagnose", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "diagnose", action: "start" } },
      { kind: "tool", id: "complete-diagnose", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "diagnose", action: "complete", evidence: [{ id: "diagnosis", kind: "note", summary: "expiry missing" }] } },
      { kind: "tool", id: "start-design", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "design", action: "start" } },
      { kind: "tool", id: "complete-design", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "design", action: "complete", evidence: [{ id: "boundary", kind: "note", summary: "[issuedAt, expiresAt)" }] } },
      { kind: "tool", id: "start-implement", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "implement", action: "start" } },
      { kind: "tool", id: "modify", name: ToolName.MODIFY_CODE, arguments: { path: "src/token.ts", oldText: "export const expiry = false;", newText: "export const expiry = true;", planStepId: "implement" } },
      { kind: "tool", id: "complete-implement", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "implement", action: "complete", evidenceIds: ["tool:modify:3"] } },
      { kind: "tool", id: "start-verify", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "verify", action: "start" } },
      { kind: "tool", id: "test", name: ToolName.RUN_TESTS, arguments: { script: "test", planStepId: "verify" } },
      { kind: "tool", id: "build", name: ToolName.RUN_TESTS, arguments: { script: "build", planStepId: "verify" } },
      { kind: "tool", id: "complete-verify", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "repair", stepId: "verify", action: "complete", evidenceIds: ["tool:test:4", "tool:build:5"] } },
      { kind: "final", text: "repair completed" },
    ], fixture.context, {
      planStore: store,
      planId: "repair",
      runtime,
      completion: { kind: "fix", requiredPlanId: "repair" },
      maxTurns: 16,
    });

    expect(await readFile(tokenPath, "utf8")).toContain("expiry = true");
    expect(runtime.getHandlerExecutionCount(ToolName.MODIFY_CODE)).toBe(1);
    expect(runtime.getHandlerExecutionCount(ToolName.RUN_TESTS)).toBe(2);
    expect(store.get("repair").steps.every((step) => step.status === StepStatus.COMPLETED)).toBe(true);
    expect(store.get("repair").evidence).toEqual(expect.arrayContaining([
      expect.objectContaining({ toolCallId: "modify", stepId: "implement", kind: "change" }),
      expect.objectContaining({ toolCallId: "test", stepId: "verify", kind: "test" }),
      expect.objectContaining({ toolCallId: "build", stepId: "verify", kind: "test" }),
    ]));
    expect(result.state.stopReason).toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
  });

  it("dynamically revises a diagnosis-only plan into a verification branch and executes its real tests", async () => {
    const fixture = await createFixture();
    await writeFile(join(fixture.repositoryRoot, "package.json"), JSON.stringify({
      scripts: { test: "node -e \"process.exit(0)\"", build: "node -e \"process.exit(0)\"" },
    }), "utf8");
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("dynamic", [{ id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION }]);
    const runtime = new ToolRuntime({ planStore: store });
    const observed: Context[] = [];

    const result = await run([
      { kind: "tool", id: "start-diagnose", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "diagnose", action: "start" } },
      { kind: "tool", id: "complete-diagnose", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "diagnose", action: "complete", evidence: [{ id: "found", kind: "note", summary: "implementation already correct" }] } },
      { kind: "tool", id: "revise", name: ToolName.REVISE_PLAN, arguments: { planId: "dynamic", expectedVersion: 1, reason: "no implementation gap", evidenceIds: ["found"], replacements: [], additions: [
        { id: "verify-tests", title: "verify tests", kind: "verification", dependsOn: ["diagnose"] },
        { id: "verify-build", title: "verify build", kind: "verification", dependsOn: ["verify-tests"] },
      ] } },
      { kind: "tool", id: "start-tests", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "verify-tests", action: "start" } },
      { kind: "tool", id: "test", name: ToolName.RUN_TESTS, arguments: { script: "test", planStepId: "verify-tests" } },
      { kind: "tool", id: "complete-tests", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "verify-tests", action: "complete", evidenceIds: ["tool:test:2"] } },
      { kind: "tool", id: "start-build", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "verify-build", action: "start" } },
      { kind: "tool", id: "build", name: ToolName.RUN_TESTS, arguments: { script: "build", planStepId: "verify-build" } },
      { kind: "tool", id: "complete-build", name: ToolName.UPDATE_PLAN_STEP, arguments: { planId: "dynamic", stepId: "verify-build", action: "complete", evidenceIds: ["tool:build:3"] } },
      { kind: "final", text: "dynamic plan completed" },
    ], fixture.context, { planStore: store, planId: "dynamic", runtime, completion: { kind: "fix", requiredPlanId: "dynamic" }, maxTurns: 14 }, observed);

    expect(runtime.getHandlerExecutionCount(ToolName.MODIFY_CODE)).toBe(0);
    expect(runtime.getHandlerExecutionCount(ToolName.RUN_TESTS)).toBe(2);
    expect(store.get("dynamic").revisions).toEqual([expect.objectContaining({ addedStepIds: ["verify-tests", "verify-build"] })]);
    expect(store.get("dynamic").steps.every((step) => step.status === StepStatus.COMPLETED)).toBe(true);
    expect(result.state.stopReason).toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
    expect(observed.some((context) => context.messages.some((message) => message.role === "user" && typeof message.content === "string" && message.content.includes("verify-tests")))).toBe(true);
  });

  it("tells a repair agent to start and execute server-ready steps after an early final", async () => {
    const fixture = await createFixture();
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("repair", [{ id: "verify", title: "run verification", kind: StepKind.VERIFICATION }]);
    const observed: Context[] = [];

    await run([{ kind: "final", text: "stopped after replanning" }], fixture.context, {
      planStore: store,
      planId: "repair",
      completion: { kind: "fix", requiredPlanId: "repair" },
      maxTurns: 1,
    }, observed);

    expect(observed).toHaveLength(1);
    // The follow-up is queued after the first natural final and will be present on the next
    // model request when the turn budget permits; inspect the StopCenter state via a two-turn run.
    const continued: Context[] = [];
    await run([{ kind: "final", text: "first" }, { kind: "final", text: "second" }], fixture.context, {
      planStore: store,
      planId: "repair",
      completion: { kind: "fix", requiredPlanId: "repair" },
      maxTurns: 2,
    }, continued);
    expect(continued[1].messages).toContainEqual(expect.objectContaining({
      role: "user",
      content: expect.stringContaining("server-ready pending steps must be started"),
    }));
  });

  it("blocks run_tests on a running investigation step before the Runtime and prevents a test Evidence from polluting diagnosis", async () => {
    const fixture = await createFixture();
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("repair", [{ id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION }]);
    store.start("repair", "diagnose");
    const runtime = new ToolRuntime({ planStore: store });

    const result = await run([
      { kind: "tool", id: "wrong-phase-test", name: ToolName.RUN_TESTS, arguments: { script: "test", planStepId: "diagnose" } },
    ], fixture.context, { planStore: store, planId: "repair", runtime, maxTurns: 2 });

    expect(runtime.getHandlerExecutionCount(ToolName.RUN_TESTS)).toBe(0);
    expect(store.get("repair").evidence).toEqual([]);
    expect(result.messages).toContainEqual(expect.objectContaining({ role: "toolResult", toolCallId: "wrong-phase-test", isError: true }));
    const blockedResult = result.messages.find((message) => message.role === "toolResult" && message.toolCallId === "wrong-phase-test");
    expect(blockedResult).toMatchObject({
      role: "toolResult",
      isError: true,
      content: [expect.objectContaining({ text: expect.stringContaining("PLAN_STEP_KIND_MISMATCH") })],
    });
  });

  it("does not classify a budget stop as completed even when a plan exists", async () => {
    const fixture = await createFixture();
    const store = new PlanStore("v1");
    store.create("plan-1", [{ id: "deliver", title: "write delivery" }]);
    store.start("plan-1", "deliver");
    const result = await run([
      { kind: "tool", id: "search", name: ToolName.SEARCH_CODE, arguments: { query: "authenticate" } },
    ], fixture.context, { planStore: store, planId: "plan-1", maxTurns: 1 });

    expect(result.state.stopReason).toBe(StopReason.MAX_TURNS_REACHED);
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
