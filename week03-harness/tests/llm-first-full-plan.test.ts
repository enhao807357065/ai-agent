import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { Context } from "@earendil-works/pi-ai";
import { runCodebaseAgent } from "../src/agent/codebase-agent-run.js";
import { codebaseAgentModel } from "../src/model/gateway-models.js";
import { PlanStore, StepKindVerifier } from "../src/planning/plan-store.js";
import { RunPlanContext } from "../src/planning/run-plan-context.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolRuntime } from "../src/runtime/tool-runtime.js";
import { createScriptedStream } from "./scripted-stream.js";

const temporary: string[] = [];
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))); });

describe("LLM-first full-plan integration", () => {
  it("creates an active full Plan before execution, then uses its snapshot to govern the next turn", async () => {
    const root = await mkdtemp(join(tmpdir(), "week03-full-plan-"));
    temporary.push(root);
    const repositoryRoot = join(root, "repo");
    await mkdir(repositoryRoot);
    await writeFile(join(repositoryRoot, "package.json"), "{}", "utf8");
    await writeFile(join(repositoryRoot, "token.ts"), "export const token = true;\n", "utf8");
    const context = createRunExecutionContext({ projectRoot: root, repositoryRoot, artifactsRoot: join(root, "artifacts"), taskArtifactPath: "report.md" });
    const planStore = new PlanStore("v1", new StepKindVerifier());
    const runPlan = new RunPlanContext();
    const runtime = new ToolRuntime({ planStore, runPlanContext: runPlan });
    const observed: Context[] = [];

    await runCodebaseAgent({
      model: codebaseAgentModel,
      streamFn: createScriptedStream([
        { kind: "tool", id: "create", name: ToolName.CREATE_PLAN, arguments: {
          planId: "full-plan", goal: "inspect token", steps: [
            { id: "inspect", title: "inspect", kind: "investigation" },
            { id: "implement", title: "implement", kind: "implementation", dependsOn: ["inspect"] },
            { id: "verify", title: "verify", kind: "verification", dependsOn: ["implement"] },
          ],
        } },
        { kind: "final", text: "done" },
      ], observed),
      executionContext: context,
      runtime,
      planStore,
      runPlanContext: runPlan,
      completion: { kind: "fix" },
      maxTurns: 2,
    });

    expect(runPlan.activePlanId()).toBe("full-plan");
    expect(planStore.get("full-plan").steps).toHaveLength(3);
    expect(observed[0].messages.some((message) => typeof message.content === "string" && message.content.startsWith("[PLAN_STATE]"))).toBe(false);
    expect(observed[1].messages).toContainEqual(expect.objectContaining({ role: "user", content: expect.stringContaining("full-plan") }));
  });
});
