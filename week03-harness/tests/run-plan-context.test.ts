import { describe, expect, it } from "vitest";
import { RunPlanContext } from "../src/planning/run-plan-context.js";

describe("RunPlanContext", () => {
  it("activates exactly one created plan and never silently switches the Run to another plan", () => {
    const context = new RunPlanContext();

    expect(context.activePlanId()).toBeUndefined();
    context.activate("plan-first");

    expect(context.activePlanId()).toBe("plan-first");
    expect(() => context.activate("plan-second")).toThrow("PLAN_ALREADY_ACTIVE");
    expect(context.activePlanId()).toBe("plan-first");
  });
});
