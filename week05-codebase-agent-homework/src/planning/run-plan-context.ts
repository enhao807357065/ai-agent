import type { PlanStore } from "./plan-store.js";

/** Run-scoped plan pointer; switching plans implicitly is intentionally forbidden. */
export class RunPlanContext {
  #activePlanId: string | undefined;

  activePlanId(): string | undefined { return this.#activePlanId; }
  hasActivePlan(): boolean { return this.#activePlanId !== undefined; }

  activate(planId: string, store?: PlanStore): void {
    if (!planId) throw new Error("PLAN_ID_REQUIRED");
    if (this.#activePlanId) throw new Error("PLAN_ALREADY_ACTIVE");
    store?.get(planId);
    this.#activePlanId = planId;
  }

  restore(planId: string | undefined, store: PlanStore): void {
    if (planId) this.activate(planId, store);
  }
}
