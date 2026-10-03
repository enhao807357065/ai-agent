import type {
  AfterToolCallContext,
  BeforeToolCallContext,
  ShouldStopAfterTurnContext,
} from "@earendil-works/pi-agent-core";
import { describe, expect, it } from "vitest";
import {
  actionFingerprint,
  createRunStopHooks,
  RunStopCenter,
  StopReason,
} from "../src/agent/run-stop-center.js";
import { PlanStore, StepKind, StepKindVerifier } from "../src/planning/plan-store.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolResultCode, ToolRuntime } from "../src/runtime/tool-runtime.js";
import { createPiRuntimeTools } from "../src/tools/pi-runtime-tools.js";

const completion = {
  minimumReadFiles: 3,
  requiredArtifactPath: "login-flow.md",
};

describe("RunStopCenter", () => {
  it("blocks the third semantically identical action before the Runtime handler, including reordered nested parameters", async () => {
    const runtime = new ToolRuntime();
    const context = createRunExecutionContext();
    const center = createCenter();
    const tool = createPiRuntimeTools(runtime, context).find(({ name }) => name === ToolName.LIST_FILES)!;

    for (const parameters of [
      { startPath: "." },
      { startPath: "." },
    ]) {
      const before = center.beforeToolCall(beforeContext(ToolName.LIST_FILES, parameters));
      expect(before).toBeUndefined();
      await tool.execute(`call-${Math.random()}`, parameters);
    }

    const third = center.beforeToolCall(beforeContext(ToolName.LIST_FILES, { startPath: "." }));

    expect(third).toMatchObject({
      block: true,
      terminate: true,
      reason: expect.stringContaining("[repeated_action]"),
    });
    expect(runtime.getHandlerExecutionCount(ToolName.LIST_FILES)).toBe(2);
    expect(center.getState().stopReason).toBe(StopReason.REPEATED_ACTION);
  });

  it("canonicalizes reordered nested object parameters to one action fingerprint", () => {
    expect(actionFingerprint("search_code", { query: "login", filter: { b: 2, a: 1 } })).toBe(
      actionFingerprint("search_code", { filter: { a: 1, b: 2 }, query: "login" }),
    );
  });

  it("rejects a natural Final when successful source-read evidence is missing", () => {
    const center = createCenter();

    expect(center.shouldStopAfterTurn(turnContext([]))).toBe(false);

    const followUp = center.takeFollowUpMessages();
    expect(followUp).toHaveLength(1);
    expect(followUp[0]).toMatchObject({
      role: "user",
      content: expect.stringContaining("[FINAL_REJECTED]"),
    });
    expect((followUp[0] as { content: string }).content).toContain("Read 3 more related source file(s)");
    expect((followUp[0] as { content: string }).content).toContain("login-flow.md");
  });

  it("does not count error results as evidence and rejects a Final when only the artifact is missing", () => {
    const center = createCenter();
    center.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/routes/login.ts" }, true, ToolResultCode.OK));
    center.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/services/auth-service.ts" }, false, ToolResultCode.OK));
    center.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/repositories/user-repository.ts" }, false, ToolResultCode.OK));
    center.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/db/database.ts" }, false, ToolResultCode.OK));

    expect(center.getState().readSourceFiles).toEqual([
      "src/db/database.ts",
      "src/repositories/user-repository.ts",
      "src/services/auth-service.ts",
    ]);
    expect(center.getMissingCompletionEvidence()).toEqual([
      "Write the required login-flow artifact: login-flow.md.",
    ]);
    expect(center.shouldStopAfterTurn(turnContext([]))).toBe(false);
    expect((center.takeFollowUpMessages()[0] as { content: string }).content).toContain("Write the required login-flow artifact: login-flow.md.");
  });

  it("ends successfully only after three successful related reads and the required artifact write", () => {
    const center = createCenter();
    for (const path of [
      "src/routes/login.ts",
      "src/services/auth-service.ts",
      "src/repositories/user-repository.ts",
    ]) {
      center.afterToolCall(afterContext(ToolName.READ_FILE, { path }, false, ToolResultCode.OK));
    }
    center.afterToolCall(afterContext(
      ToolName.WRITE_ARTIFACT,
      { path: "login-flow.md", content: "flow" },
      false,
      ToolResultCode.OK,
      "login-flow.md",
    ));

    expect(center.getMissingCompletionEvidence()).toEqual([]);
    expect(center.shouldStopAfterTurn(turnContext([]))).toBe(true);
    expect(center.getState().stopReason).toBe(StopReason.COMPLETION_CONTRACT_SATISFIED);
  });

  it("blocks apply_patch before Runtime when its planStepId is not a running implementation step", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("guard", [{ id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION }]);
    store.start("guard", "diagnose");
    const center = new RunStopCenter({ runId: "guard", maxTurns: 4, completion: { kind: "fix", requiredPlanId: "guard" }, planStore: store, planId: "guard" });

    const decision = center.beforeToolCall(beforeContext(ToolName.APPLY_PATCH, {
      planStepId: "diagnose",
      patches: [{ path: "src/auth.ts", oldText: "old", newText: "new" }],
    }));

    expect(decision).toMatchObject({ block: true, reason: expect.stringContaining("PLAN_STEP_KIND_MISMATCH") });
  });

  it("records only successful read/search calls as note-evidence sources", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("trace", [{ id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION }]);
    store.start("trace", "diagnose");
    const center = new RunStopCenter({ runId: "trace", maxTurns: 4, completion: { kind: "fix", requiredPlanId: "trace" }, planStore: store, planId: "trace" });
    center.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/token.ts" }, false, ToolResultCode.OK));
    store.recordEvidence("trace", "diagnose", [{ id: "source-backed", kind: "note", summary: "observed", sourceToolCallIds: ["tool-call"] }]);
    expect(store.get("trace").evidence[0].sourceToolCallIds).toEqual(["tool-call"]);
  });

  it("allows evidence-backed replacement skips to satisfy the fix contract only after the replacement finishes", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("repair", [
      { id: "old", title: "invalid hypothesis", kind: StepKind.INVESTIGATION },
      { id: "verify", title: "verification", kind: StepKind.VERIFICATION, dependsOn: ["old"] },
    ]);
    store.start("repair", "old");
    store.recordEvidence("repair", "old", [{ id: "why", kind: "note", summary: "new path" }]);
    const revised = store.revise("repair", {
      expectedVersion: 1,
      reason: "replace invalid hypothesis",
      evidenceIds: ["why"],
      replacements: [{ replaceStepId: "old", step: { id: "new", title: "correct diagnosis", kind: StepKind.INVESTIGATION } }],
    });
    store.start("repair", "new");
    store.recordEvidence("repair", "new", [{ id: "note", kind: "note", summary: "done" }]);
    store.complete("repair", "new", ["note"]);
    store.start("repair", "verify");
    store.recordEvidence("repair", "verify", [{ id: "test", kind: "test", summary: "passed" }]);
    store.complete("repair", "verify", ["test"]);
    expect(revised.steps.find((step) => step.id === "old")?.status).toBe("skipped");
    const center = new RunStopCenter({ runId: "replacement", maxTurns: 4, completion: { kind: "fix", requiredPlanId: "repair" }, planStore: store, planId: "repair" });
    expect(center.getMissingCompletionEvidence()).toEqual([]);
  });

  it("sets the maximum-turn stop reason before another model call and does not override an earlier reason", () => {
    const center = new RunStopCenter({ runId: "run-limit", maxTurns: 2, completion });

    expect(center.shouldStopAfterTurn(turnContext([toolResultMessage()]))).toBe(false);
    expect(center.shouldStopAfterTurn(turnContext([toolResultMessage()]))).toBe(true);
    expect(center.getState()).toMatchObject({
      currentTurn: 2,
      stopReason: StopReason.MAX_TURNS_REACHED,
    });
    expect(center.shouldStopAfterTurn(turnContext([]))).toBe(true);
    expect(center.getState().stopReason).toBe(StopReason.MAX_TURNS_REACHED);
  });

  it("exports Pi hook functions that preserve the same center across Follow-up turns", async () => {
    const center = createCenter();
    const hooks = createRunStopHooks(center);

    await hooks.afterToolCall(afterContext(ToolName.READ_FILE, { path: "src/routes/login.ts" }, false, ToolResultCode.OK));
    expect(await hooks.shouldStopAfterTurn(turnContext([]))).toBe(false);
    expect(await hooks.getFollowUpMessages()).toHaveLength(1);
    expect(center.getState().currentTurn).toBe(1);
  });
});

function createCenter(): RunStopCenter {
  return new RunStopCenter({ runId: "run-1", maxTurns: 8, completion });
}

function beforeContext(toolName: string, args: unknown): BeforeToolCallContext {
  return {
    assistantMessage: assistantMessage(),
    toolCall: { type: "toolCall", id: "tool-call", name: toolName, arguments: args as Record<string, unknown> },
    args,
    context: agentContext(),
  };
}

function afterContext(
  toolName: string,
  args: unknown,
  isError: boolean,
  code: string,
  artifactPath?: string,
): AfterToolCallContext {
  return {
    ...beforeContext(toolName, args),
    result: {
      content: [{ type: "text", text: "governed" }],
      details: { code, ...(artifactPath === undefined ? {} : { artifactPath }) },
    },
    isError,
  };
}

function turnContext(toolResults: unknown[]): ShouldStopAfterTurnContext {
  return {
    message: assistantMessage(),
    toolResults: toolResults as ShouldStopAfterTurnContext["toolResults"],
    context: agentContext(),
    newMessages: [],
  };
}

function assistantMessage() {
  return {
    role: "assistant" as const,
    content: [],
    api: "openai-completions" as const,
    provider: "gateway",
    model: "codebase-agent",
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "stop" as const,
    timestamp: 0,
  };
}

function agentContext() {
  return { systemPrompt: "test", messages: [] };
}

function toolResultMessage() {
  return {
    role: "toolResult" as const,
    toolCallId: "call",
    toolName: ToolName.LIST_FILES,
    content: [{ type: "text" as const, text: "ok" }],
    details: { code: ToolResultCode.OK },
    isError: false,
    timestamp: 0,
  };
}
