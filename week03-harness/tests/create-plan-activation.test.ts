import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { RunPlanContext } from "../src/planning/run-plan-context.js";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import { ToolName, ToolRuntime } from "../src/runtime/tool-runtime.js";

const temporary: string[] = [];
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))); });

describe("create_plan activation", () => {
  it("activates the exact plan only after the Runtime successfully creates it", async () => {
    const root = await mkdtemp(join(tmpdir(), "week03-create-plan-"));
    temporary.push(root);
    const repositoryRoot = join(root, "repo");
    await mkdir(repositoryRoot);
    await writeFile(join(repositoryRoot, "package.json"), "{}", "utf8");
    const context = createRunExecutionContext({ projectRoot: root, repositoryRoot, artifactsRoot: join(root, "artifacts"), taskArtifactPath: "report.md" });
    const runPlan = new RunPlanContext();
    const runtime = new ToolRuntime({ runPlanContext: runPlan });

    const result = await runtime.execute({
      toolCallId: "create-1",
      toolName: ToolName.CREATE_PLAN,
      modelArguments: {
        planId: "repair",
        goal: "repair token boundary",
        steps: [{ id: "inspect", title: "inspect", kind: "investigation" }],
      },
      context,
    });

    expect(result.success).toBe(true);
    expect(runPlan.activePlanId()).toBe("repair");
  });
});
