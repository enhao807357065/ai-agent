import { describe, expect, it } from "vitest";
import {
  CurrentVersionStepVerifier,
  EvidenceKind,
  PlanStore,
  PlanStoreError,
  StepKind,
  StepKindVerifier,
  StepStatus,
} from "../src/planning/plan-store.js";
import { PlanToolHandlers } from "../src/planning/plan-tools.js";

describe("PlanStore", () => {
  it("starts only dependency-ready steps; failed and skipped dependencies never satisfy readiness", () => {
    const store = new PlanStore("v1");
    store.create("p", [{ id: "a", title: "A" }, { id: "b", title: "B", dependsOn: ["a"] }]);
    expect(store.readySteps("p").map((step) => step.id)).toEqual(["a"]);
    expect(() => store.start("p", "b")).toThrow(/cannot start/u);
    store.start("p", "a"); store.fail("p", "a", "broken");
    expect(store.readySteps("p")).toEqual([]);
  });

  it("completes only running steps with same-step, current-version test and change evidence", () => {
    const store = new PlanStore("v2");
    store.create("p", [{ id: "a", title: "A" }, { id: "b", title: "B" }]);
    store.start("p", "a");
    store.recordEvidence("p", "a", [{ id: "t", kind: EvidenceKind.TEST, summary: "passed" }, { id: "c", kind: EvidenceKind.CHANGE, summary: "implementation" }]);
    expect(() => store.complete("p", "a", ["t"])) .toThrow(/change evidence/u);
    expect(() => store.complete("p", "a", ["t", "c", "missing"])) .toThrow(/does not exist/u);
    store.recordEvidence("p", "b", [{ id: "other", kind: EvidenceKind.CHANGE, summary: "wrong owner" }]);
    expect(() => store.complete("p", "a", ["t", "c", "other"])) .toThrow(/belong/u);
    expect(store.complete("p", "a", ["t", "c"]).steps[0].status).toBe(StepStatus.COMPLETED);

    let version = "old";
    const stale = new PlanStore(() => version); stale.create("stale", [{ id: "x", title: "X" }]); stale.start("stale", "x"); stale.recordEvidence("stale", "x", [{ id: "t", kind: EvidenceKind.TEST, summary: "old test" }, { id: "c", kind: EvidenceKind.CHANGE, summary: "old change" }]);
    version = "new";
    expect(() => stale.complete("stale", "x", ["t", "c"])) .toThrow(/older code version/u);
  });

  it("validates unique IDs, existing dependencies, and DAG cycles before storing a plan", () => {
    const store = new PlanStore("v1");
    expect(() => store.create("dupe", [{ id: "a", title: "A" }, { id: "a", title: "Again" }])).toThrow(/unique/u);
    expect(() => store.create("missing", [{ id: "a", title: "A", dependsOn: ["nope"] }])).toThrow(/does not exist/u);
    expect(() => store.create("cycle", [{ id: "a", title: "A", dependsOn: ["b"] }, { id: "b", title: "B", dependsOn: ["a"] }])).toThrow(/acyclic/u);
  });

  it("revises atomically with optimistic version, evidence, bounded revisions, retained skipped old step and rewired downstream", () => {
    const store = new PlanStore("v1", undefined, 1);
    store.create("p", [{ id: "a", title: "old" }, { id: "b", title: "downstream", dependsOn: ["a"] }]);
    store.start("p", "a"); store.block("p", "a", "need replacement");
    store.recordEvidence("p", "a", [{ id: "why", kind: EvidenceKind.NOTE, summary: "failure evidence" }]);
    expect(() => store.revise("p", { expectedVersion: 99, reason: "retry", evidenceIds: ["why"], replacements: [] })).toThrow(/expectedVersion/u);
    const revised = store.revise("p", { expectedVersion: 1, reason: "retry", evidenceIds: ["why"], replacements: [{ replaceStepId: "a", step: { id: "a2", title: "new" } }] });
    expect(revised.version).toBe(2);
    expect(revised.revisions[0]).toMatchObject({ replacedStepIds: ["a"], addedStepIds: [] });
    expect(revised.steps.find((step) => step.id === "a")?.status).toBe(StepStatus.SKIPPED);
    expect(revised.steps.find((step) => step.id === "b")?.dependsOn).toEqual(["a2"]);
    expect(() => store.revise("p", { expectedVersion: 2, reason: "again", evidenceIds: ["why"], replacements: [] })).toThrow(/limit/u);
  });

  it("rejects a revision whose candidate dependency graph contains a cycle without publishing partial state", () => {
    const store = new PlanStore("v1");
    store.create("p", [{ id: "a", title: "old" }, { id: "b", title: "downstream", dependsOn: ["a"] }]);
    store.start("p", "a");
    store.block("p", "a", "need rethink");
    store.recordEvidence("p", "a", [{ id: "why", kind: EvidenceKind.NOTE, summary: "diagnosis" }]);

    expect(() => store.revise("p", {
      expectedVersion: 1,
      reason: "replacement would cycle",
      evidenceIds: ["why"],
      replacements: [{ replaceStepId: "a", step: { id: "a2", title: "new", dependsOn: ["b"] } }],
    })).toThrow(/acyclic/u);
    expect(store.get("p")).toMatchObject({ version: 1 });
    expect(store.get("p").steps.find((step) => step.id === "a")?.status).toBe(StepStatus.BLOCKED);
  });

  it("uses step-kind evidence policies to unlock a repair DAG without weakening legacy code-delivery verification", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("repair", [
      { id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION },
      { id: "design", title: "design", kind: StepKind.DESIGN, dependsOn: ["diagnose"] },
      { id: "implement", title: "implement", kind: StepKind.IMPLEMENTATION, dependsOn: ["design"] },
      { id: "verify", title: "verify", kind: StepKind.VERIFICATION, dependsOn: ["implement"] },
    ]);
    store.start("repair", "diagnose");
    store.recordEvidence("repair", "diagnose", [{ id: "d", kind: EvidenceKind.NOTE, summary: "token has no expiry" }]);
    store.complete("repair", "diagnose", ["d"]);
    store.start("repair", "design");
    store.recordEvidence("repair", "design", [{ id: "s", kind: EvidenceKind.NOTE, summary: "[issuedAt, expiresAt)" }]);
    store.complete("repair", "design", ["s"]);
    store.start("repair", "implement");
    store.recordEvidence("repair", "implement", [{ id: "c", kind: EvidenceKind.CHANGE, summary: "token.ts" }]);
    store.complete("repair", "implement", ["c"]);
    store.start("repair", "verify");
    store.recordEvidence("repair", "verify", [{ id: "t", kind: EvidenceKind.TEST, summary: "boundary passed" }]);
    expect(store.complete("repair", "verify", ["t"]).steps.every((step) => step.status === StepStatus.COMPLETED)).toBe(true);

    const legacy = new PlanStore("v1", new CurrentVersionStepVerifier());
    legacy.create("legacy", [{ id: "fix", title: "fix" }]);
    legacy.start("legacy", "fix");
    legacy.recordEvidence("legacy", "fix", [{ id: "note", kind: EvidenceKind.NOTE, summary: "not enough" }]);
    expect(() => legacy.complete("legacy", "fix", ["note"])).toThrow(/test evidence/u);
  });

  it("adds an evidence-backed verification branch after diagnosis without forcing a fictitious implementation step", () => {
    const store = new PlanStore("v1", new StepKindVerifier());
    store.create("dynamic", [{ id: "diagnose", title: "diagnose", kind: StepKind.INVESTIGATION }]);
    store.start("dynamic", "diagnose");
    store.recordEvidence("dynamic", "diagnose", [{ id: "found", kind: EvidenceKind.NOTE, summary: "existing implementation already meets the expiry boundary" }]);
    store.complete("dynamic", "diagnose", ["found"]);

    const revised = store.revise("dynamic", {
      expectedVersion: 1,
      reason: "Diagnosis found no implementation gap; add verification-only delivery branch.",
      evidenceIds: ["found"],
      replacements: [],
      additions: [
        { id: "verify-tests", title: "run boundary tests", kind: StepKind.VERIFICATION, dependsOn: ["diagnose"] },
        { id: "verify-build", title: "run build", kind: StepKind.VERIFICATION, dependsOn: ["verify-tests"] },
      ],
    });

    expect(revised.revisions[0]).toMatchObject({ replacedStepIds: [], addedStepIds: ["verify-tests", "verify-build"] });
    expect(store.readySteps("dynamic").map((step) => step.id)).toEqual(["verify-tests"]);
    expect(() => store.revise("dynamic", {
      expectedVersion: 2,
      reason: "empty revision",
      evidenceIds: ["found"],
      replacements: [],
    })).toThrow(/must replace or add/u);
  });

  it("tracks note sources, plan intent, and real code-version evolution", () => {
    const version = { value: "v0" };
    const store = new PlanStore(() => version.value, new StepKindVerifier());
    store.create("trace", [{
      id: "diagnose",
      title: "inspect source",
      kind: StepKind.INVESTIGATION,
      successCriteria: ["cite a successful source read"],
    }], {
      goal: "Audit expiry behavior",
      constraints: ["Do not modify code before diagnosis"],
    });
    store.recordObservationToolCall("read-token");
    store.start("trace", "diagnose");
    store.recordEvidence("trace", "diagnose", [{
      id: "observed",
      kind: EvidenceKind.NOTE,
      summary: "token parser checked",
      sourceToolCallIds: ["read-token"],
    }]);
    const plan = store.get("trace");
    expect(plan).toMatchObject({ goal: "Audit expiry behavior", constraints: ["Do not modify code before diagnosis"] });
    expect(plan.steps[0].successCriteria).toEqual(["cite a successful source read"]);
    expect(plan.evidence[0].sourceToolCallIds).toEqual(["read-token"]);
    expect(() => store.recordEvidence("trace", "diagnose", [{ id: "forged", kind: EvidenceKind.NOTE, summary: "no source", sourceToolCallIds: ["unknown"] }])).toThrow(/successful read_file or search_code/u);
  });

  it("keeps stale verification evidence from completing a changed implementation", () => {
    let version = "v0";
    const store = new PlanStore(() => version, new StepKindVerifier());
    store.create("versions", [{ id: "implement", title: "change", kind: StepKind.IMPLEMENTATION }]);
    store.start("versions", "implement");
    store.recordEvidence("versions", "implement", [{ id: "change", kind: EvidenceKind.CHANGE, summary: "first change" }]);
    version = "v1";
    expect(() => store.complete("versions", "implement", ["change"])).toThrow(/older code version/u);
  });

  it("keeps handlers as the only state mutation boundary", () => {
    const handlers = new PlanToolHandlers(new PlanStore("v1"));
    handlers.createPlan({ planId: "p", steps: [{ id: "a", title: "A" }] });
    const afterStart = handlers.updatePlanStep({ planId: "p", stepId: "a", action: "start" });
    expect(afterStart.steps[0].status).toBe(StepStatus.RUNNING);
    const completed = handlers.updatePlanStep({ planId: "p", stepId: "a", action: "complete", evidence: [{ id: "t", kind: EvidenceKind.TEST, summary: "pass" }, { id: "c", kind: EvidenceKind.CHANGE, summary: "change" }] });
    expect(completed.steps[0].status).toBe(StepStatus.COMPLETED);
  });
});
