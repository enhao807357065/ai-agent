import type { Plan, NewPlanStep, RevisePlanInput } from "./plan-store.js";
import type { PlanStore } from "./plan-store.js";
import type { RunPlanContext } from "./run-plan-context.js";

/** Narrow command boundary for planner-capable adapters. Server-owned fields never cross this API. */
export class PlanToolHandlers {
  constructor(private readonly store: PlanStore, private readonly context: RunPlanContext) {}

  createPlan(id: string, steps: readonly NewPlanStep[], metadata: { goal: string; constraints?: readonly string[] }): Plan {
    const plan = this.store.create(id, steps, metadata);
    this.context.activate(plan.id, this.store);
    return plan;
  }
  getPlan(): Plan { return this.store.get(this.id()); }
  startStep(stepId: string): Plan { return this.store.start(this.id(), stepId); }
  completeStep(stepId: string, evidenceIds: readonly string[]): Plan {
    const plan = this.store.get(this.id());
    // 模型若不指定 ID，只能自动绑定服务端已经为本步骤记下的可信工具证据。
    const trustedEvidenceIds = evidenceIds.length > 0
      ? evidenceIds
      : plan.evidence.filter((evidence) => evidence.stepId === stepId).map((evidence) => evidence.id);
    return this.store.complete(plan.id, stepId, trustedEvidenceIds);
  }
  revisePlan(input: RevisePlanInput): Plan { return this.store.revise(this.id(), input); }

  private id(): string {
    const id = this.context.activePlanId();
    if (!id) throw new Error("NO_ACTIVE_PLAN");
    return id;
  }
}
