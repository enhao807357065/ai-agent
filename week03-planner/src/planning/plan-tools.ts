import { Type, type Static } from "typebox";
import {
  EvidenceKind,
  PlanStore,
  StepKind,
  StepStatus,
  type NewPlanStep,
  type Plan,
  type RevisePlanInput,
} from "./plan-store.js";

/**
 * LLM-facing input schema layer。
 *
 * 这些 Schema 只限定“模型可提出什么形状的请求”，不代表模型可直接写状态。
 * 真正的状态转换固定发生在 PlanToolHandlers → PlanStore；这样工具调用与
 * HTTP/API 调用未来可复用相同的领域校验边界。
 */
const stepInput = Type.Object({
  id: Type.String({ minLength: 1 }),
  title: Type.String({ minLength: 1 }),
  successCriteria: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  kind: Type.Optional(Type.Union([
    Type.Literal(StepKind.INVESTIGATION), Type.Literal(StepKind.DESIGN),
    Type.Literal(StepKind.IMPLEMENTATION), Type.Literal(StepKind.VERIFICATION),
  ])),
  dependsOn: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
}, { additionalProperties: false });

/** Evidence 输入故意不含 stepId 和 codeVersion：二者分别由当前 Handler 与可信版本源注入。 */
const evidenceInput = Type.Object({
  id: Type.String({ minLength: 1 }),
  kind: Type.Union([
    Type.Literal(EvidenceKind.TEST),
    Type.Literal(EvidenceKind.CHANGE),
    Type.Literal(EvidenceKind.NOTE),
  ]),
  summary: Type.String({ minLength: 1 }),
  sourceToolCallIds: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
}, { additionalProperties: false });

export const createPlanParameters = Type.Object({
  planId: Type.String({ minLength: 1 }),
  goal: Type.Optional(Type.String({ minLength: 1 })),
  constraints: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  steps: Type.Array(stepInput, { minItems: 1 }),
}, { additionalProperties: false });

export const getPlanParameters = Type.Object({
  planId: Type.String({ minLength: 1 }),
}, { additionalProperties: false });

/**
 * 一个工具承载有限的步骤动作；`complete` 仍只是“申请完成”。
 * Handler 会先记录本次提供的 evidence，再交由 PlanStore 校验归属、版本和 StepVerifier。
 */
export const updatePlanStepParameters = Type.Object({
  planId: Type.String({ minLength: 1 }),
  stepId: Type.String({ minLength: 1 }),
  action: Type.Union([
    Type.Literal("start"),
    Type.Literal("complete"),
    Type.Literal("fail"),
    Type.Literal("block"),
    Type.Literal("skip"),
  ]),
  evidence: Type.Optional(Type.Array(evidenceInput)),
  evidenceIds: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  reason: Type.Optional(Type.String({ minLength: 1 })),
}, { additionalProperties: false });

/** revise 使用 expectedVersion 进行乐观锁；Store 内部再原子重接依赖图。 */
export const revisePlanParameters = Type.Object({
  planId: Type.String({ minLength: 1 }),
  expectedVersion: Type.Integer({ minimum: 1 }),
  reason: Type.String({ minLength: 1 }),
  evidenceIds: Type.Array(Type.String({ minLength: 1 })),
  replacements: Type.Array(Type.Object({
    replaceStepId: Type.String({ minLength: 1 }),
    step: stepInput,
  }, { additionalProperties: false })),
  additions: Type.Optional(Type.Array(stepInput)),
}, { additionalProperties: false });

export type CreatePlanParameters = Static<typeof createPlanParameters>;
export type GetPlanParameters = Static<typeof getPlanParameters>;
export type UpdatePlanStepParameters = Static<typeof updatePlanStepParameters>;
export type RevisePlanParameters = Static<typeof revisePlanParameters>;

/** 供 Runtime 注册和 Pi adapter 暴露的稳定模型工具名称。 */
export const PlanToolName = {
  CREATE_PLAN: "create_plan",
  GET_PLAN: "get_plan",
  UPDATE_PLAN_STEP: "update_plan_step",
  REVISE_PLAN: "revise_plan",
} as const;
export type PlanToolName = (typeof PlanToolName)[keyof typeof PlanToolName];

/**
 * PlanStore 的应用层 Handler。
 *
 * 依赖注入作用：Handler 只依赖 PlanStore 抽象，不关心它是内存、SQLite 还是远程数据库。
 * 责任边界：Handler 负责把一个已通过 Schema 的工具提议映射为领域命令；Store 负责
 * 所有状态机、不变量、版本和依赖图验证。因此模型永远不能提交一份“目标 Plan 状态”。
 */
export class PlanToolHandlers {
  constructor(private readonly store: PlanStore) {}

  createPlan(input: CreatePlanParameters): Plan {
    return this.store.create(input.planId, input.steps as NewPlanStep[], {
      goal: input.goal ?? input.planId,
      constraints: input.constraints ?? [],
    });
  }

  getPlan(input: GetPlanParameters): Plan {
    return this.store.get(input.planId);
  }

  /**
   * 关键时序：
   * - start / fail / block / skip：直接提交受限状态命令；
   * - complete：可先登记新证据，再发起 complete；Store 最后决定是否真正变为 completed。
   */
  updatePlanStep(input: UpdatePlanStepParameters): Plan {
    switch (input.action) {
      case "start":
        return this.store.start(input.planId, input.stepId);
      case "complete": {
        if (input.evidence?.length) {
          this.store.recordEvidence(input.planId, input.stepId, input.evidence);
        }
        // 没有合格证据时 Store 会拒绝；不能把 action="complete" 当作直接改状态的权限。
        return this.store.complete(
          input.planId,
          input.stepId,
          input.evidenceIds ?? input.evidence?.map((item) => item.id) ?? [],
        );
      }
      case "fail":
        return this.store.fail(input.planId, input.stepId, requireReason(input.reason));
      case "block":
        return this.store.block(input.planId, input.stepId, requireReason(input.reason));
      case "skip":
        return this.store.skip(input.planId, input.stepId, requireReason(input.reason));
    }
  }

  revisePlan(input: RevisePlanParameters): Plan {
    return this.store.revise(input.planId, input satisfies RevisePlanInput);
  }
}

/** Runtime 的 ControlledToolResult 使用 string content，因此当前最小版本将 Plan 序列化为 JSON。 */
export function serializePlan(plan: Plan): string {
  return JSON.stringify(plan);
}

/** fail / block / skip 必须留下可解释原因；Schema 的 optional 是为了与其它 action 共用一个契约。 */
function requireReason(reason: string | undefined): string {
  if (!reason) throw new Error("A reason is required for fail, block, and skip.");
  return reason;
}

/**
 * 工具注册元数据的单一来源。
 * 当前 Runtime 使用各项分别注册以保留已有结构；此数组便于未来改为 Registry / Snapshot 时复用。
 */
export const ALL_PLAN_TOOL_SCHEMAS = [
  { name: PlanToolName.CREATE_PLAN, schema: createPlanParameters },
  { name: PlanToolName.GET_PLAN, schema: getPlanParameters },
  { name: PlanToolName.UPDATE_PLAN_STEP, schema: updatePlanStepParameters },
  { name: PlanToolName.REVISE_PLAN, schema: revisePlanParameters },
] as const;

export { StepStatus };
