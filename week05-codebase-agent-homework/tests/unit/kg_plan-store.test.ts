import { describe, expect, it } from "vitest";
import { mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { PlatformStore } from "../../src/store.js";
import { CodeVersionTracker } from "../../src/planning/code-version.js";
import { EvidenceKind, PlanStore, StepKind, StepStatus } from "../../src/planning/plan-store.js";

describe("durable PlanStore", () => {
  it("persists a validated DAG and its evidence across a new store instance", () => {
    const database = join(mkdtempSync(join(tmpdir(), "plan-store-")), "platform.db");
    const versions = new CodeVersionTracker();
    const first = new PlanStore(new PlatformStore(database), versions);
    first.create("plan-1", [
      { id: "inspect", title: "Inspect", kind: StepKind.INVESTIGATION },
      { id: "change", title: "Change", kind: StepKind.IMPLEMENTATION, dependsOn: ["inspect"] },
    ], { goal: "make a safe change" });
    first.start("plan-1", "inspect");
    first.recordEvidence("plan-1", "inspect", [{ id: "note-1", kind: EvidenceKind.NOTE, summary: "read source" }]);
    first.complete("plan-1", "inspect", ["note-1"]);

    const restored = new PlanStore(new PlatformStore(database), versions).get("plan-1");
    expect(restored.steps.map((step) => step.status)).toEqual([StepStatus.COMPLETED, StepStatus.PENDING]);
    expect(restored.evidence[0]).toMatchObject({ id: "note-1", codeVersion: "run-code-0" });
    expect(new PlanStore(new PlatformStore(database), versions).readySteps("plan-1").map((step) => step.id)).toEqual(["change"]);
  });

  it("fails closed when a step depends on a missing node", () => {
    const store = new PlanStore(new PlatformStore(join(mkdtempSync(join(tmpdir(), "plan-store-")), "platform.db")), new CodeVersionTracker());
    expect(() => store.create("bad", [{ id: "one", title: "one", dependsOn: ["missing"] }])).toThrowError(expect.objectContaining({ code: "DEPENDENCY_NOT_FOUND" }));
  });
});
