import { PlanStore } from "./plan-store.js";

/**
 * 一次 Run 只能绑定一份活跃 Plan。PlanStore 可以保存多个 Plan，但模型成功创建
 * Plan 后，必须由 Harness 显式 activate；后续 Step Gate、Evidence、Snapshot 和
 * Completion 都读取这里的指针，避免不同模块各自猜测当前该治理哪份 Plan。
 */
export class RunPlanContext {
  #activePlanId: string | undefined;

  activePlanId(): string | undefined {
    return this.#activePlanId;
  }

  hasActivePlan(): boolean {
    return this.#activePlanId !== undefined;
  }

  /**
   * 激活发生在 create_plan 已经被 PlanStore 成功校验并写入之后。一个 Run 不允许
   * 静默切换到另一份 Plan；未来若确有切换需求，应新增可审计的显式领域操作。
   */
  activate(planId: string, store?: PlanStore): void {
    if (!planId) throw new Error("PLAN_ID_REQUIRED");
    if (this.#activePlanId !== undefined) {
      throw new Error("PLAN_ALREADY_ACTIVE");
    }
    if (store) store.get(planId);
    this.#activePlanId = planId;
  }
}
