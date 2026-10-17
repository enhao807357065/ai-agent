import { describe, expect, it } from "vitest";
import { PlanToolHandlers } from "../src/planning/plan-tools.js";
import { EvidenceKind, PlanStore, StepKind, StepKindVerifier, StepStatus } from "../src/planning/plan-store.js";

describe("additions-only replan", () => {
  it("accepts omitted replacements, advances version, and exposes the first appended step as ready after diagnosis", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    const handlers = new PlanToolHandlers(store);
    handlers.createPlan({ planId: "p", steps: [{ id: "diagnose", title: "诊断", kind: StepKind.INVESTIGATION }] });
    store.start("p", "diagnose");
    handlers.updatePlanStep({
      planId: "p", stepId: "diagnose", action: "complete",
      evidence: [{ id: "observed", kind: EvidenceKind.NOTE, summary: "根因已定位" }],
    });
    const revised = handlers.revisePlan({
      planId: "p", expectedVersion: 1, reason: "诊断确认需要修复", evidenceIds: ["observed"],
      additions: [{ id: "implement", title: "修复", kind: StepKind.IMPLEMENTATION, dependsOn: ["diagnose"] }],
    });
    expect(revised.version).toBe(2);
    expect(revised.revisions[0]?.addedStepIds).toEqual(["implement"]);
    expect(revised.steps.find((step) => step.id === "diagnose")?.status).toBe(StepStatus.COMPLETED);
    expect(store.readySteps("p").map((step) => step.id)).toEqual(["implement"]);
  });
});
