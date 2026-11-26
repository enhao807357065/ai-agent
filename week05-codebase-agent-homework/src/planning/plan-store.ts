import { createHash } from "node:crypto";
import type { PlatformStore } from "../store.js";

export const StepStatus = {
  PENDING: "pending",
  RUNNING: "running",
  COMPLETED: "completed",
  FAILED: "failed",
  BLOCKED: "blocked",
  SKIPPED: "skipped",
} as const;
export type StepStatus = (typeof StepStatus)[keyof typeof StepStatus];

export const StepKind = {
  INVESTIGATION: "investigation",
  DESIGN: "design",
  IMPLEMENTATION: "implementation",
  VERIFICATION: "verification",
} as const;
export type StepKind = (typeof StepKind)[keyof typeof StepKind];

export const EvidenceKind = { TEST: "test", CHANGE: "change", NOTE: "note" } as const;
export type EvidenceKind = (typeof EvidenceKind)[keyof typeof EvidenceKind];

export interface PlanStep {
  readonly id: string;
  readonly title: string;
  readonly successCriteria: readonly string[];
  readonly kind: StepKind;
  readonly dependsOn: readonly string[];
  readonly status: StepStatus;
  readonly completionEvidenceIds: readonly string[];
  readonly failureReason?: string;
  readonly blockedReason?: string;
}

export interface Evidence {
  readonly id: string;
  readonly stepId: string;
  readonly kind: EvidenceKind;
  readonly summary: string;
  readonly toolCallId?: string;
  readonly sourceToolCallIds: readonly string[];
  readonly codeVersion: string;
  readonly workspaceDigest?: string;
}

export interface VerificationOutcome {
  readonly toolCallId: string;
  readonly scope: string;
  readonly testedDigest: string;
  readonly passed: boolean;
  readonly failureCode?: string;
}

export interface PlanRevision {
  readonly version: number;
  readonly reason: string;
  readonly evidenceIds: readonly string[];
  readonly replacedStepIds: readonly string[];
  readonly addedStepIds: readonly string[];
}

export interface Plan {
  readonly id: string;
  readonly goal: string;
  readonly constraints: readonly string[];
  readonly version: number;
  readonly steps: readonly PlanStep[];
  readonly evidence: readonly Evidence[];
  readonly verificationOutcomes: readonly VerificationOutcome[];
  readonly revisions: readonly PlanRevision[];
}

export interface NewPlanStep {
  readonly id: string;
  readonly title: string;
  readonly successCriteria?: readonly string[];
  readonly kind?: StepKind;
  readonly dependsOn?: readonly string[];
}

export interface PlanMetadata { readonly goal: string; readonly constraints?: readonly string[] }
export interface EvidenceInput {
  readonly id: string;
  readonly kind: EvidenceKind;
  readonly summary: string;
  readonly sourceToolCallIds?: readonly string[];
  readonly workspaceDigest?: string;
}
export interface ToolEvidenceInput {
  readonly toolCallId: string;
  readonly kind: EvidenceKind;
  readonly summary: string;
  readonly workspaceDigest?: string;
}
export interface RevisePlanInput {
  readonly expectedVersion: number;
  readonly reason: string;
  readonly evidenceIds: readonly string[];
  readonly replacements: readonly { readonly replaceStepId: string; readonly step: NewPlanStep }[];
  readonly additions?: readonly NewPlanStep[];
}

export class PlanStoreError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "PlanStoreError";
  }
}

/** SQLite-backed Plan DAG aggregate. All transitions write a canonical snapshot in one Store transaction. */
export class PlanStore {
  constructor(
    private readonly store: PlatformStore,
    private readonly currentCodeVersion: string | (() => string) | { current(): string },
  ) {}

  create(id: string, steps: readonly NewPlanStep[], metadata: PlanMetadata = { goal: id }): Plan {
    if (this.store.getPlan(id)) throw new PlanStoreError("PLAN_EXISTS", `Plan ${id} already exists.`);
    const plan = normalize({
      id,
      goal: metadata.goal,
      constraints: [...(metadata.constraints ?? [])],
      version: 1,
      steps: steps.map((step) => newStep(step)),
      evidence: [],
      verificationOutcomes: [],
      revisions: [],
    });
    validateGraph(plan.steps);
    return this.save(plan);
  }

  get(planId: string): Plan {
    const row = this.store.getPlan(planId);
    if (!row) throw new PlanStoreError("PLAN_NOT_FOUND", `Plan ${planId} does not exist.`);
    return parsePlan(row.plan_json, row.checksum);
  }

  readySteps(planId: string): readonly PlanStep[] {
    const plan = this.get(planId);
    return plan.steps.filter((step) => step.status === StepStatus.PENDING && step.dependsOn.every((id) => findStep(plan, id).status === StepStatus.COMPLETED));
  }

  start(planId: string, stepId: string): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.PENDING || !this.readySteps(planId).some((item) => item.id === stepId)) {
      throw new PlanStoreError("STEP_NOT_READY", `Step ${stepId} cannot start before dependencies complete.`);
    }
    return this.replace(plan, { ...step, status: StepStatus.RUNNING });
  }

  recordEvidence(planId: string, stepId: string, inputs: readonly EvidenceInput[]): Plan {
    const plan = this.get(planId);
    findStep(plan, stepId);
    const ids = new Set(plan.evidence.map((item) => item.id));
    for (const input of inputs) {
      if (ids.has(input.id)) throw new PlanStoreError("EVIDENCE_EXISTS", `Evidence ${input.id} already exists.`);
      ids.add(input.id);
    }
    return this.save({
      ...plan,
      evidence: [...plan.evidence, ...inputs.map((input) => ({
        id: input.id,
        kind: input.kind,
        summary: input.summary,
        sourceToolCallIds: [...(input.sourceToolCallIds ?? [])],
        workspaceDigest: input.workspaceDigest,
        stepId,
        codeVersion: this.codeVersion(),
      }))],
    });
  }

  recordToolEvidence(planId: string, stepId: string, input: ToolEvidenceInput): Evidence {
    const plan = this.get(planId);
    findStep(plan, stepId);
    const evidence: Evidence = {
      id: `tool:${input.toolCallId}:${plan.evidence.length + 1}`,
      stepId,
      kind: input.kind,
      summary: input.summary,
      toolCallId: input.toolCallId,
      sourceToolCallIds: [],
      codeVersion: this.codeVersion(),
      workspaceDigest: input.workspaceDigest,
    };
    if (plan.evidence.some((item) => item.id === evidence.id)) throw new PlanStoreError("EVIDENCE_EXISTS", "Tool evidence already exists.");
    this.save({ ...plan, evidence: [...plan.evidence, evidence] });
    return evidence;
  }

  recordVerificationOutcome(planId: string, outcome: VerificationOutcome): Plan {
    if (!outcome.toolCallId || !outcome.scope || !outcome.testedDigest) throw new PlanStoreError("VERIFICATION_OUTCOME_INVALID", "Verification requires call ID, scope and digest.");
    const plan = this.get(planId);
    if (plan.verificationOutcomes.some((item) => item.toolCallId === outcome.toolCallId)) throw new PlanStoreError("VERIFICATION_OUTCOME_EXISTS", "Verification outcome already exists.");
    return this.save({ ...plan, verificationOutcomes: [...plan.verificationOutcomes, outcome] });
  }

  complete(planId: string, stepId: string, evidenceIds: readonly string[]): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.RUNNING) throw new PlanStoreError("STEP_NOT_RUNNING", `Step ${stepId} is not running.`);
    const evidence = evidenceIds.map((id) => findEvidence(plan, id));
    if (evidence.some((item) => item.stepId !== stepId)) throw new PlanStoreError("EVIDENCE_STEP_MISMATCH", "Evidence belongs to another step.");
    verifyStep(step, evidence, this.codeVersion());
    return this.replace(plan, { ...step, status: StepStatus.COMPLETED, completionEvidenceIds: [...evidenceIds] });
  }

  fail(planId: string, stepId: string, reason: string): Plan { return this.transition(planId, stepId, StepStatus.FAILED, reason); }
  block(planId: string, stepId: string, reason: string): Plan { return this.transition(planId, stepId, StepStatus.BLOCKED, reason); }
  skip(planId: string, stepId: string, reason: string): Plan { return this.transition(planId, stepId, StepStatus.SKIPPED, reason); }

  revise(planId: string, input: RevisePlanInput): Plan {
    const plan = this.get(planId);
    if (plan.version !== input.expectedVersion) throw new PlanStoreError("PLAN_VERSION_CONFLICT", "Plan version does not match expectedVersion.");
    input.evidenceIds.forEach((id) => findEvidence(plan, id));
    const additions = input.additions ?? [];
    if (input.replacements.length === 0 && additions.length === 0) throw new PlanStoreError("EMPTY_REVISION", "A revision must add or replace a step.");
    const replaced = new Map(input.replacements.map((item) => [item.replaceStepId, item.step.id]));
    input.replacements.forEach((item) => {
      if (findStep(plan, item.replaceStepId).status === StepStatus.COMPLETED) throw new PlanStoreError("STEP_ALREADY_COMPLETED", "Completed steps cannot be replaced.");
    });
    const added = [...input.replacements.map((item) => item.step), ...additions];
    const candidate: Plan = {
      ...plan,
      version: plan.version + 1,
      steps: [
        ...plan.steps.map((step) => replaced.has(step.id)
          ? { ...step, status: StepStatus.SKIPPED, blockedReason: `Revised: ${input.reason}` }
          : { ...step, dependsOn: step.dependsOn.map((id) => replaced.get(id) ?? id) }),
        ...added.map(newStep),
      ],
      revisions: [...plan.revisions, {
        version: plan.version + 1,
        reason: input.reason,
        evidenceIds: [...input.evidenceIds],
        replacedStepIds: input.replacements.map((item) => item.replaceStepId),
        addedStepIds: added.map((item) => item.id),
      }],
    };
    validateGraph(candidate.steps);
    return this.save(candidate);
  }

  completionIssues(planId: string): readonly string[] {
    const plan = this.get(planId);
    const current = this.codeVersion();
    const issues: string[] = [];
    for (const step of plan.steps.filter((item) => item.kind === StepKind.VERIFICATION && item.status === StepStatus.COMPLETED)) {
      for (const evidenceId of step.completionEvidenceIds) {
        const evidence = findEvidence(plan, evidenceId);
        if (evidence.kind === EvidenceKind.TEST && evidence.codeVersion !== current) issues.push(`Verification evidence ${evidence.id} is stale.`);
        if (evidence.kind === EvidenceKind.TEST && evidence.toolCallId && !plan.verificationOutcomes.some((outcome) => outcome.toolCallId === evidence.toolCallId)) issues.push(`Verification evidence ${evidence.id} has no tested-content outcome.`);
      }
    }
    const latest = new Map<string, VerificationOutcome>();
    for (const outcome of plan.verificationOutcomes) latest.set(outcome.scope, outcome);
    for (const outcome of latest.values()) if (!outcome.passed) issues.push(`Latest verification ${outcome.scope} failed at ${outcome.toolCallId}.`);
    return Object.freeze([...new Set(issues)]);
  }

  snapshot(planId: string): string { return JSON.stringify(this.get(planId)); }

  private transition(planId: string, stepId: string, status: StepStatus, reason: string): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.RUNNING) throw new PlanStoreError("STEP_NOT_RUNNING", `Step ${stepId} is not running.`);
    return this.replace(plan, status === StepStatus.FAILED ? { ...step, status, failureReason: reason } : { ...step, status, blockedReason: reason });
  }

  private replace(plan: Plan, replacement: PlanStep): Plan {
    return this.save({ ...plan, steps: plan.steps.map((step) => step.id === replacement.id ? replacement : step) });
  }

  private save(plan: Plan): Plan {
    const normalized = normalize(plan);
    this.store.savePlan(normalized.id, JSON.stringify(normalized), checksum(normalized));
    return normalized;
  }

  private codeVersion(): string {
    if (typeof this.currentCodeVersion === "function") return this.currentCodeVersion();
    if (typeof this.currentCodeVersion === "string") return this.currentCodeVersion;
    return this.currentCodeVersion.current();
  }
}

function newStep(step: NewPlanStep): PlanStep {
  return { id: step.id, title: step.title, successCriteria: [...(step.successCriteria ?? [])], kind: step.kind ?? StepKind.IMPLEMENTATION, dependsOn: [...(step.dependsOn ?? [])], status: StepStatus.PENDING, completionEvidenceIds: [] };
}
function findStep(plan: Plan, id: string): PlanStep {
  const step = plan.steps.find((item) => item.id === id);
  if (!step) throw new PlanStoreError("STEP_NOT_FOUND", `Step ${id} does not exist.`);
  return step;
}
function findEvidence(plan: Plan, id: string): Evidence {
  const evidence = plan.evidence.find((item) => item.id === id);
  if (!evidence) throw new PlanStoreError("EVIDENCE_NOT_FOUND", `Evidence ${id} does not exist.`);
  return evidence;
}
function verifyStep(step: PlanStep, evidence: readonly Evidence[], version: string): void {
  const requireKind = (kind: EvidenceKind) => {
    if (!evidence.some((item) => item.kind === kind)) throw new PlanStoreError("STEP_EVIDENCE_KIND_REQUIRED", `${step.kind} requires ${kind} evidence.`);
  };
  if (step.kind === StepKind.INVESTIGATION || step.kind === StepKind.DESIGN) {
    requireKind(EvidenceKind.NOTE);
    return;
  }
  if (step.kind === StepKind.IMPLEMENTATION) requireKind(EvidenceKind.CHANGE);
  if (step.kind === StepKind.VERIFICATION) requireKind(EvidenceKind.TEST);
  if (evidence.some((item) => item.codeVersion !== version)) throw new PlanStoreError("STALE_EVIDENCE", "Evidence was produced by an older code version.");
}
function validateGraph(steps: readonly PlanStep[]): void {
  const byId = new Map(steps.map((step) => [step.id, step]));
  if (byId.size !== steps.length) throw new PlanStoreError("DUPLICATE_STEP_ID", "Step IDs must be unique.");
  const visiting = new Set<string>(); const visited = new Set<string>();
  const visit = (id: string): void => {
    if (visiting.has(id)) throw new PlanStoreError("DEPENDENCY_CYCLE", "Plan dependency graph must be acyclic.");
    if (visited.has(id)) return;
    const step = byId.get(id);
    if (!step) throw new PlanStoreError("DEPENDENCY_NOT_FOUND", `Dependency ${id} does not exist.`);
    visiting.add(id); step.dependsOn.forEach(visit); visiting.delete(id); visited.add(id);
  };
  steps.forEach((step) => visit(step.id));
}
function checksum(value: unknown): string { return createHash("sha256").update(JSON.stringify(value)).digest("hex"); }
function parsePlan(json: string, expectedChecksum: string): Plan {
  const value = JSON.parse(json) as Plan;
  if (checksum(value) !== expectedChecksum) throw new PlanStoreError("PLAN_CHECKSUM_MISMATCH", "Stored plan integrity check failed.");
  validateGraph(value.steps);
  return normalize(value);
}
function normalize(plan: Plan): Plan {
  return Object.freeze({
    ...plan,
    constraints: Object.freeze([...plan.constraints]),
    steps: Object.freeze(plan.steps.map((step) => Object.freeze({ ...step, successCriteria: Object.freeze([...step.successCriteria]), dependsOn: Object.freeze([...step.dependsOn]), completionEvidenceIds: Object.freeze([...step.completionEvidenceIds]) }))),
    evidence: Object.freeze(plan.evidence.map((evidence) => Object.freeze({ ...evidence, sourceToolCallIds: Object.freeze([...evidence.sourceToolCallIds]) }))),
    verificationOutcomes: Object.freeze([...plan.verificationOutcomes]),
    revisions: Object.freeze(plan.revisions.map((revision) => Object.freeze({ ...revision, evidenceIds: Object.freeze([...revision.evidenceIds]), replacedStepIds: Object.freeze([...revision.replacedStepIds]), addedStepIds: Object.freeze([...revision.addedStepIds]) }))),
  });
}
