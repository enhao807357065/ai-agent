/**
 * Planner 的领域状态模型。
 *
 * 模型只会通过工具“提出”创建、推进或修订计划的请求；PlanStore 才是唯一可以
 * 改写计划状态的领域对象。当前实现使用进程内 Map，方便学习和单进程 Demo；
 * 生产环境可在不改变 Handler 契约的前提下替换为数据库实现。
 */
export const StepStatus = {
  /** 尚未开始；只有所有依赖均完成时才可能进入 running。 */
  PENDING: "pending",
  /** 已被调度执行，允许记录证据并申请 complete / fail / block / skip。 */
  RUNNING: "running",
  /** 已通过 StepVerifier 的交付验收；这是唯一能满足下游依赖的状态。 */
  COMPLETED: "completed",
  FAILED: "failed",
  BLOCKED: "blocked",
  /** 步骤不会删除，修订时保留旧步骤并以 skipped 留下审计轨迹。 */
  SKIPPED: "skipped",
} as const;

export type StepStatus = (typeof StepStatus)[keyof typeof StepStatus];

/** 一条计划步骤：依赖图与步骤状态共同决定是否可调度。 */
export const StepKind = {
  INVESTIGATION: "investigation",   // 调查
  DESIGN: "design",                 // 设计
  IMPLEMENTATION: "implementation", // 实施
  VERIFICATION: "verification",     // 验证
} as const;
export type StepKind = (typeof StepKind)[keyof typeof StepKind];

export interface PlanStep {
  readonly id: string;
  readonly title: string;
  /** 面向模型与审计者的可观察验收标准；服务端仍由 StepVerifier 执行硬校验。 */
  readonly successCriteria: readonly string[];
  /** 验收策略的服务端分类；不再把诊断步骤按“代码修改”规则验收。 */
  readonly kind: StepKind;
  readonly dependsOn: readonly string[];
  readonly status: StepStatus;
  /** 已被本步骤完成申请消费的证据 ID，便于追溯交付依据。 */
  readonly completionEvidenceIds: readonly string[];
  readonly failureReason?: string;
  readonly blockedReason?: string;
}

export const EvidenceKind = {
  TEST: "test",     // 验证结果事实，证明修改后行为被实际验证过
  CHANGE: "change", // 受控变更事实，证明系统中确实发生了某个变更
  NOTE: "note", // 观察/理解事实，证明看到了什么、确认了什么
} as const;

export type EvidenceKind = (typeof EvidenceKind)[keyof typeof EvidenceKind];

/**
 * 证据必须绑定到具体步骤与可信代码版本。
 * codeVersion 不存在于模型工具参数中，而由 Store 使用服务端版本源盖章，
 * 防止模型把旧代码版本的测试通过结果伪装成当前交付的证据。
 */
export interface Evidence {
  readonly id: string;
  readonly stepId: string;      // 归属的step
  readonly kind: EvidenceKind;
  readonly summary: string;
  /** 由 afterToolCall 绑定，证明该证据来自哪次真实受控工具执行。 */
  readonly toolCallId?: string;
  /** note 必须可回溯到同一 Run 中成功的 read/search Tool Call；test/change 由工具本身产生。 */
  readonly sourceToolCallIds: readonly string[];
  readonly codeVersion: string;
}

/**
 * 验证结果与被测内容的绑定。codeVersion 只表达本 Run 内的受控变更顺序；
 * testedDigest 才能证明某个 npm 脚本实际针对哪份工作区内容执行。
 * 同一 scope 只认最后一次结果，避免“曾经通过”遮住后续失败。
 */
export interface VerificationOutcome {
  readonly toolCallId: string;
  readonly scope: string;
  readonly testedDigest: string;
  readonly passed: boolean;
  readonly failureCode?: string;
}

/** 每一次计划修订的最小审计记录；旧步骤不会被覆盖或删除。 */
export interface PlanRevision {
  readonly version: number;
  readonly reason: string;
  readonly evidenceIds: readonly string[];
  /** 被替代的旧步骤会保留为 skipped，不能从审计图中消失。 */
  readonly replacedStepIds: readonly string[];
  /** Evidence 驱动的动态扩展：诊断完成后才追加真实实施/验证分支。 */
  readonly addedStepIds: readonly string[];
}

/** 一个 Plan 是版本化的、不可变快照式的聚合根。 */
export interface Plan {
  readonly id: string;
  /** 用户任务的可审计目标；不是 System Prompt 中的隐式示例业务。 */
  readonly goal: string;
  /** 服务端/用户明确给出的边界，例如禁止修改、允许脚本或目录范围。 */
  readonly constraints: readonly string[];
  /** 乐观锁版本。修订者必须带上自己读取时看到的版本。 */
  readonly version: number;
  readonly steps: readonly PlanStep[];
  readonly evidence: readonly Evidence[];
  /** 追加式验证账本；每个 scope 的最后一条才是当前有效结论。旧 checkpoint 可缺失。 */
  readonly verificationOutcomes?: readonly VerificationOutcome[];
  readonly revisions: readonly PlanRevision[];
}

/** 创建或替换步骤时允许的模型提议字段；运行态字段只能由 Store 补全。 */
export interface NewPlanStep {
  readonly id: string;
  readonly title: string;
  /** 人类和模型都能读到的逐步验收目标；不能替代服务端 StepVerifier。 */
  readonly successCriteria?: readonly string[];
  readonly kind?: StepKind;
  readonly dependsOn?: readonly string[];
}

export interface PlanMetadata {
  readonly goal: string;
  readonly constraints?: readonly string[];
}

/** 模型可描述证据类型和摘要，但不能自行填写可信 codeVersion 或 stepId。 */
export interface EvidenceInput {
  readonly id: string;
  readonly kind: EvidenceKind;
  readonly summary: string;
  /** note 必须引用本 Run 内已经成功执行过的 read_file/search_code 调用。 */
  readonly sourceToolCallIds?: readonly string[];
}

/** Runtime 只产生该草稿；afterToolCall 才能把已验证的 step、call ID 和版本写为真实 Evidence。 */
export interface EvidenceDraft {
  readonly kind: EvidenceKind;
  readonly summary: string;
}

/**
 * afterToolCall 的专用入口：调用方不能覆盖 stepId / codeVersion，
 * 只能把 Runtime 的草稿与 Pi 已验证的 toolCallId 一起提交。
 */
export interface ToolEvidenceInput extends EvidenceDraft {
  readonly toolCallId: string;
}

export interface RevisePlanInput {
  readonly expectedVersion: number;
  readonly reason: string;
  /** 修订本身也必须可追溯到已有证据。 */
  readonly evidenceIds: readonly string[];
  /** 替换步骤会追加到图中；被替换步骤保留并被标记 skipped。 */
  readonly replacements: readonly {
    readonly replaceStepId: string;
    readonly step: NewPlanStep;
  }[];
  /**
   * 不推翻既有步骤、只根据诊断发现扩展 DAG 时使用。
   * 例如 diagnosis 完成后，再追加“实现+验证”或“仅验证”分支。
   */
  readonly additions?: readonly NewPlanStep[];
}

/**
 * StepVerifier 是“步骤能否交付”的可注入策略。
 * PlanStore 固定负责状态机、证据归属和依赖图；不同领域可替换验证策略，
 * 例如文档步骤可能只需 review evidence，而代码交付需要 test + change。
 */
export interface StepVerifier {
  verify(input: {
    readonly plan: Plan;
    readonly step: PlanStep;
    readonly evidence: readonly Evidence[];
    readonly currentCodeVersion: string;
  }): void;
}

/** 最小代码交付策略：必须有当前代码版本产生的测试与改动两类证据。 */
export class CurrentVersionStepVerifier implements StepVerifier {
  verify({ evidence, currentCodeVersion }: Parameters<StepVerifier["verify"]>[0]): void {
    if (!evidence.some((item) => item.kind === EvidenceKind.TEST)) {
      throw new PlanStoreError("STEP_EVIDENCE_TEST_REQUIRED", "Completion requires test evidence.");
    }
    if (!evidence.some((item) => item.kind === EvidenceKind.CHANGE)) {
      throw new PlanStoreError("STEP_EVIDENCE_CHANGE_REQUIRED", "Completion requires change evidence.");
    }
    if (evidence.some((item) => item.codeVersion !== currentCodeVersion)) {
      throw new PlanStoreError("STALE_EVIDENCE", "Delivery evidence was produced by an older code version.");
    }
  }
}

/** 会话修复等多阶段任务的默认策略：每类步骤只接受与其产出匹配的真实证据。 */
export class StepKindVerifier implements StepVerifier {
  verify({ step, evidence, currentCodeVersion }: Parameters<StepVerifier["verify"]>[0]): void {
    const requireKind = (kind: EvidenceKind): void => {
      if (!evidence.some((item) => item.kind === kind)) {
        throw new PlanStoreError("STEP_EVIDENCE_KIND_REQUIRED", `${step.kind} completion requires ${kind} evidence.`);
      }
    };
    if (step.kind === StepKind.INVESTIGATION || step.kind === StepKind.DESIGN) {
      requireKind(EvidenceKind.NOTE);
      return;
    }
    if (step.kind === StepKind.IMPLEMENTATION) requireKind(EvidenceKind.CHANGE);
    if (step.kind === StepKind.VERIFICATION) requireKind(EvidenceKind.TEST);
    if (evidence.some((item) => item.codeVersion !== currentCodeVersion)) {
      throw new PlanStoreError("STALE_EVIDENCE", "Delivery evidence was produced by an older code version.");
    }
  }
}

/** 领域错误携带稳定 code；Runtime 会把它归一化成对模型可见的受控失败结果。 */
export class PlanStoreError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "PlanStoreError";
  }
}

/**
 * 单进程内存 PlanStore。
 *
 * 依赖注入说明：
 * - currentCodeVersion 是服务端可信版本源；使用函数时，可在证据写入后模拟代码变更；
 * - verifier 是交付验收策略；Store 不将“什么算合格”硬编码到状态机；
 * - maxRevisions 是防止 Agent 在失败时无限 replan 的安全阀。
 */
export class PlanStore {
  readonly #plans = new Map<string, Plan>();
  readonly #knownObservationToolCallIds = new Set<string>();

  constructor(
    private readonly currentCodeVersion: string | (() => string),
    private readonly verifier: StepVerifier = new CurrentVersionStepVerifier(),
    private readonly maxRevisions = 3,
  ) {}

  /** 创建前校验 ID、依赖存在性和无环性；成功后所有步骤从 pending 开始。 */
  create(id: string, steps: readonly NewPlanStep[], metadata: PlanMetadata = { goal: id }): Plan {
    if (this.#plans.has(id)) throw new PlanStoreError("PLAN_EXISTS", `Plan ${id} already exists.`);
    const plan = freezePlan({
      id,
      goal: metadata.goal,
      constraints: [...(metadata.constraints ?? [])],
      version: 1,
      steps: steps.map((step) => ({
        id: step.id,
        title: step.title,
        successCriteria: [...(step.successCriteria ?? [])],
        kind: step.kind ?? StepKind.IMPLEMENTATION,
        dependsOn: [...(step.dependsOn ?? [])],
        status: StepStatus.PENDING,
        completionEvidenceIds: [],
      })),
      evidence: [],
      verificationOutcomes: [],
      revisions: [],
    });
    validateGraph(plan.steps);
    this.#plans.set(id, plan);
    return plan;
  }

  /** 在 resume 前用已持久化的完整 Plan 重新建立内存 Store；不能直接篡改 #plans。 */
  restore(plan: Plan): Plan {
    if (this.#plans.has(plan.id)) throw new PlanStoreError("PLAN_EXISTS", `Plan ${plan.id} already exists.`);
    validateGraph(plan.steps);
    const restored = freezePlan({
      ...plan,
      constraints: [...plan.constraints],
      steps: plan.steps.map((step) => ({ ...step, successCriteria: [...step.successCriteria], dependsOn: [...step.dependsOn], completionEvidenceIds: [...step.completionEvidenceIds] })),
      evidence: plan.evidence.map((evidence) => ({ ...evidence, sourceToolCallIds: [...evidence.sourceToolCallIds] })),
      verificationOutcomes: [...(plan.verificationOutcomes ?? [])],
      revisions: plan.revisions.map((revision) => ({ ...revision, evidenceIds: [...revision.evidenceIds], replacedStepIds: [...revision.replacedStepIds], addedStepIds: [...revision.addedStepIds] })),
    });
    this.#plans.set(restored.id, restored);
    for (const evidence of restored.evidence) {
      for (const sourceToolCallId of evidence.sourceToolCallIds) this.#knownObservationToolCallIds.add(sourceToolCallId);
    }
    return restored;
  }

  /** 返回冻结快照，调用方不能绕过 Store 直接篡改内存中的聚合状态。 */
  get(planId: string): Plan {
    const plan = this.#plans.get(planId);
    if (!plan) throw new PlanStoreError("PLAN_NOT_FOUND", `Plan ${planId} does not exist.`);
    return plan;
  }

  /**
   * 只有 pending 且每个依赖都是 completed 的步骤可执行。
   * failed / blocked / skipped 都不会被当作“已经完成”，避免下游在前置失败时误启动。
   */
  readySteps(planId: string): readonly PlanStep[] {
    const plan = this.get(planId);
    return plan.steps.filter(
      (step) => step.status === StepStatus.PENDING
        && step.dependsOn.every((id) => findStep(plan, id).status === StepStatus.COMPLETED),
    );
  }

  /** start 再次复用 readySteps 检查，而不是相信调用方先前读取到的就绪结果。 */
  start(planId: string, stepId: string): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.PENDING || !this.readySteps(planId).some((item) => item.id === stepId)) {
      throw new PlanStoreError("STEP_NOT_READY", `Step ${stepId} cannot start before all dependencies complete.`);
    }
    return this.#replace(plan, { ...step, status: StepStatus.RUNNING });
  }

  /** afterToolCall 登记成功的只读观察；模型提交 note 时必须引用这里的真实 callId。 */
  recordObservationToolCall(toolCallId: string): void {
    if (toolCallId) this.#knownObservationToolCallIds.add(toolCallId);
  }

  /** 仅暴露本 Run 成功的只读观察 ID，供 Handler 在 note Evidence 入库时进行服务端绑定。 */
  observationToolCallIds(): readonly string[] {
    return Object.freeze([...this.#knownObservationToolCallIds]);
  }

  /** 记录证据时由 Store 补齐 owner stepId 与当前 codeVersion。
   * 两个字段不能由模型传入，避免跨步骤挪用证据和伪造版本新鲜度。
   */
  recordEvidence(planId: string, stepId: string, inputs: readonly EvidenceInput[]): Plan {
    const plan = this.get(planId);
    findStep(plan, stepId);
    const known = new Set(plan.evidence.map((item) => item.id));
    for (const input of inputs) {
      if (known.has(input.id)) throw new PlanStoreError("EVIDENCE_EXISTS", `Evidence ${input.id} already exists.`);
      if (input.kind === EvidenceKind.NOTE && input.sourceToolCallIds) {
        const sources = input.sourceToolCallIds;
        if (sources.length === 0 || sources.some((callId) => !this.#knownObservationToolCallIds.has(callId))) {
          throw new PlanStoreError("NOTE_SOURCE_NOT_FOUND", "Note evidence sourceToolCallIds must cite successful read_file or search_code tool calls from this run.");
        }
      }
      known.add(input.id);
    }
    return this.#save({
      ...plan,
      evidence: [
        ...plan.evidence,
        ...inputs.map((input) => ({
          id: input.id,
          kind: input.kind,
          summary: input.summary,
          sourceToolCallIds: [...(input.sourceToolCallIds ?? [])],
          stepId,
          codeVersion: this.#codeVersion(),
        })),
      ],
    });
  }

  /**
   * afterToolCall 专用的证据入库入口。ID 由 Store 基于真实 toolCallId 生成，
   * 所以模型不能伪造/复用某一次工具执行来覆盖另一条 Evidence。
   */
  recordToolEvidence(planId: string, stepId: string, input: ToolEvidenceInput): Evidence {
    const plan = this.get(planId);
    findStep(plan, stepId);
    const id = `tool:${input.toolCallId}:${plan.evidence.length + 1}`;
    if (plan.evidence.some((item) => item.id === id)) {
      throw new PlanStoreError("EVIDENCE_EXISTS", `Evidence ${id} already exists.`);
    }
    const evidence: Evidence = {
      id,
      stepId,
      kind: input.kind,
      summary: input.summary,
      toolCallId: input.toolCallId,
      sourceToolCallIds: [],
      codeVersion: this.#codeVersion(),
    };
    this.#save({ ...plan, evidence: [...plan.evidence, evidence] });
    return evidence;
  }

  /**
   * Runtime 在测试进程真实结束后提交结果。结果不由模型文本推断；同一 scope
   * 追加新结果后，旧结果不再代表该范围的最新验证结论。
   */
  recordVerificationOutcome(planId: string, outcome: VerificationOutcome): Plan {
    const plan = this.get(planId);
    if (!outcome.toolCallId || !outcome.scope || !outcome.testedDigest) {
      throw new PlanStoreError("VERIFICATION_OUTCOME_INVALID", "Verification outcome requires a call ID, scope and tested digest.");
    }
    if ((plan.verificationOutcomes ?? []).some((item) => item.toolCallId === outcome.toolCallId)) {
      throw new PlanStoreError("VERIFICATION_OUTCOME_EXISTS", "Verification outcome already exists for this tool call.");
    }
    return this.#save({ ...plan, verificationOutcomes: [...(plan.verificationOutcomes ?? []), outcome] });
  }

  /**
   * Run 级 Completion Contract 使用此方法，而不是只看 Step.status。一个来源文件
   * 修改后，旧 test Evidence 的 codeVersion 与当前版本不一致；同一验证 scope 的
   * 后续失败也会覆盖先前通过。返回具体缺项，供下一轮模型进行有依据的修正。
   */
  completionIssues(planId: string): readonly string[] {
    const plan = this.get(planId);
    const currentCodeVersion = this.#codeVersion();
    const issues: string[] = [];
    for (const step of plan.steps.filter((item) => item.kind === StepKind.VERIFICATION && item.status === StepStatus.COMPLETED)) {
      const evidence = step.completionEvidenceIds.map((id) => findEvidence(plan, id));
      const tests = evidence.filter((item) => item.kind === EvidenceKind.TEST);
      for (const test of tests) {
        if (test.codeVersion !== currentCodeVersion) {
          issues.push(`Verification evidence ${test.toolCallId ?? test.id} is stale: tested ${test.codeVersion}, current ${currentCodeVersion}.`);
          continue;
        }
        if (!test.toolCallId) continue;
        const outcome = [...(plan.verificationOutcomes ?? [])].reverse().find((item) => item.toolCallId === test.toolCallId);
        if (!outcome) {
          issues.push(`Verification evidence ${test.toolCallId ?? test.id} has no recorded tested-content outcome.`);
        } else if (!outcome.passed) {
          issues.push(`Latest verification ${outcome.scope} failed at ${outcome.toolCallId}${outcome.failureCode ? ` (${outcome.failureCode})` : ""}.`);
        }
      }
    }
    const latestByScope = new Map<string, VerificationOutcome>();
    for (const outcome of plan.verificationOutcomes ?? []) latestByScope.set(outcome.scope, outcome);
    for (const outcome of latestByScope.values()) {
      if (!outcome.passed) issues.push(`Latest verification ${outcome.scope} failed at ${outcome.toolCallId}${outcome.failureCode ? ` (${outcome.failureCode})` : ""}.`);
    }
    return Object.freeze([...new Set(issues)]);
  }

  /** 供 prepareNextTurn 注入；每次重新序列化当前快照，而不把旧快照追加到消息历史。 */
  snapshot(planId: string): string {
    return JSON.stringify(this.get(planId));
  }

  /**
   * 完成不是普通的 status 写入，而是一份交付申请：
   * running → 解析所有 evidence → 归属检查 → 当前版本/Verifier 校验 → completed。
   * 任一检查失败就抛错，旧快照保持不变。
   */
  complete(planId: string, stepId: string, evidenceIds: readonly string[]): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.RUNNING) throw new PlanStoreError("STEP_NOT_RUNNING", `Step ${stepId} is not running.`);

    const evidence = evidenceIds.map((id) => findEvidence(plan, id));
    if (evidence.some((item) => item.stepId !== stepId)) {
      throw new PlanStoreError("EVIDENCE_STEP_MISMATCH", "Completion evidence must belong to this step.");
    }

    this.verifier.verify({ plan, step, evidence, currentCodeVersion: this.#codeVersion() });
    return this.#replace(plan, {
      ...step,
      status: StepStatus.COMPLETED,
      completionEvidenceIds: [...evidenceIds],
    });
  }

  /** 失败/阻塞/跳过均只允许从 running 发起，避免任意覆盖已有终态。 */
  fail(planId: string, stepId: string, reason: string): Plan {
    return this.#transition(planId, stepId, StepStatus.FAILED, reason);
  }

  block(planId: string, stepId: string, reason: string): Plan {
    return this.#transition(planId, stepId, StepStatus.BLOCKED, reason);
  }

  skip(planId: string, stepId: string, reason: string): Plan {
    return this.#transition(planId, stepId, StepStatus.SKIPPED, reason);
  }

  /**
   * 原子 replan：先在内存中构造完整 candidate，再验证整个图，最后一次 #save。
   * 这样出现重复 ID、缺失依赖或环时，旧 plan 不会留下“只改了一半”的下游依赖。
   */
  revise(planId: string, input: RevisePlanInput): Plan {
    const plan = this.get(planId);

    // 1. 乐观锁与资源上限必须在构建 candidate 前失败。
    if (plan.version !== input.expectedVersion) {
      throw new PlanStoreError("PLAN_VERSION_CONFLICT", "Plan version does not match expectedVersion.");
    }
    if (plan.revisions.length >= this.maxRevisions) {
      throw new PlanStoreError("REVISION_LIMIT_REACHED", "Plan revision limit reached.");
    }

    // 2. 修订理由的支撑证据必须已经存在；这里不允许凭空引用。
    input.evidenceIds.forEach((id) => findEvidence(plan, id));

    const oldIds = new Set(input.replacements.map((item) => item.replaceStepId));
    const replacementIds = new Set(input.replacements.map((item) => item.step.id));
    const additions = input.additions ?? [];
    const addedIds = new Set(additions.map((step) => step.id));
    const allNewIds = new Set([...replacementIds, ...addedIds]);
    if (
      replacementIds.size !== input.replacements.length
      || addedIds.size !== additions.length
      || allNewIds.size !== input.replacements.length + additions.length
      || [...allNewIds].some((id) => plan.steps.some((step) => step.id === id))
    ) {
      throw new PlanStoreError("DUPLICATE_STEP_ID", "Replacement and added step IDs must be unique.");
    }
    input.replacements.forEach((item) => {
      const replacedStep = findStep(plan, item.replaceStepId);
      if (replacedStep.status === StepStatus.COMPLETED) {
        throw new PlanStoreError("STEP_ALREADY_COMPLETED", `Completed step ${item.replaceStepId} cannot be replaced.`);
      }
    });
    if (input.replacements.length === 0 && additions.length === 0) {
      throw new PlanStoreError("EMPTY_REVISION", "A revision must replace or add at least one step.");
    }

    // 3. 旧步骤留存为 skipped；所有引用旧步骤的下游依赖同步切换到新步骤。
    const replaced = new Map(input.replacements.map((item) => [item.replaceStepId, item.step.id]));
    const oldSteps = plan.steps.map((step) => oldIds.has(step.id)
      ? { ...step, status: StepStatus.SKIPPED, blockedReason: `Revised: ${input.reason}` }
      : step);
    const rewired = oldSteps.map((step) => ({
      ...step,
      dependsOn: step.dependsOn.map((dependency) => replaced.get(dependency) ?? dependency),
    }));
    const replacementSteps = input.replacements.map((item) => ({
      id: item.step.id,
      title: item.step.title,
      successCriteria: [...(item.step.successCriteria ?? [])],
      kind: item.step.kind ?? StepKind.IMPLEMENTATION,
      dependsOn: [...(item.step.dependsOn ?? [])],
      status: StepStatus.PENDING as StepStatus,
      completionEvidenceIds: [],
    }));
    const addedSteps = additions.map((step) => ({
      id: step.id,
      title: step.title,
      successCriteria: [...(step.successCriteria ?? [])],
      kind: step.kind ?? StepKind.IMPLEMENTATION,
      dependsOn: [...(step.dependsOn ?? [])],
      status: StepStatus.PENDING as StepStatus,
      completionEvidenceIds: [],
    }));

    // 4. 只有完整候选图通过 ID、依赖和 DAG 校验后，才发布新的不可变版本。
    const candidate: Plan = {
      ...plan,
      version: plan.version + 1,
      steps: [...rewired, ...replacementSteps, ...addedSteps],
      revisions: [
        ...plan.revisions,
        {
          version: plan.version + 1,
          reason: input.reason,
          evidenceIds: [...input.evidenceIds],
          replacedStepIds: [...oldIds],
          addedStepIds: [...addedIds],
        },
      ],
    };
    validateGraph(candidate.steps);
    return this.#save(candidate);
  }

  /** 统一解析可信版本源；函数形式支持“记录证据后代码发生变更”的测试和真实接入。 */
  #codeVersion(): string {
    return typeof this.currentCodeVersion === "function"
      ? this.currentCodeVersion()
      : this.currentCodeVersion;
  }

  #transition(planId: string, stepId: string, status: StepStatus, reason: string): Plan {
    const plan = this.get(planId);
    const step = findStep(plan, stepId);
    if (step.status !== StepStatus.RUNNING) {
      throw new PlanStoreError("STEP_NOT_RUNNING", `Step ${stepId} is not running.`);
    }
    return this.#replace(
      plan,
      status === StepStatus.FAILED
        ? { ...step, status, failureReason: reason }
        : { ...step, status, blockedReason: reason },
    );
  }

  /** 通过复制并冻结新快照写入，防止调用方保存对象引用后继续改写 Store 状态。 */
  #replace(plan: Plan, replacement: PlanStep): Plan {
    return this.#save({
      ...plan,
      steps: plan.steps.map((step) => step.id === replacement.id ? replacement : step),
    });
  }

  #save(plan: Plan): Plan {
    const frozen = freezePlan(plan);
    this.#plans.set(plan.id, frozen);
    return frozen;
  }
}

function findStep(plan: Plan, stepId: string): PlanStep {
  const step = plan.steps.find((item) => item.id === stepId);
  if (!step) throw new PlanStoreError("STEP_NOT_FOUND", `Step ${stepId} does not exist.`);
  return step;
}

function findEvidence(plan: Plan, evidenceId: string): Evidence {
  const item = plan.evidence.find((evidence) => evidence.id === evidenceId);
  if (!item) throw new PlanStoreError("EVIDENCE_NOT_FOUND", `Evidence ${evidenceId} does not exist.`);
  return item;
}

/** DFS 三色思想的简化实现：visiting 再次遇到自身即代表有环。 */
function validateGraph(steps: readonly PlanStep[]): void {
  const ids = new Set(steps.map((step) => step.id));
  if (ids.size !== steps.length) {
    throw new PlanStoreError("DUPLICATE_STEP_ID", "Step IDs must be unique.");
  }

  for (const step of steps) {
    for (const dependency of step.dependsOn) {
      if (!ids.has(dependency)) {
        throw new PlanStoreError("DEPENDENCY_NOT_FOUND", `Dependency ${dependency} does not exist.`);
      }
    }
  }

  const byId = new Map(steps.map((step) => [step.id, step]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string): void => {
    if (visiting.has(id)) throw new PlanStoreError("DEPENDENCY_CYCLE", "Plan dependency graph must be acyclic.");
    if (visited.has(id)) return;
    visiting.add(id);
    byId.get(id)?.dependsOn.forEach(visit);
    visiting.delete(id);
    visited.add(id);
  };
  steps.forEach((step) => visit(step.id));
}

/** 深冻结集合字段，使每一次读到的 Plan 都是一份不能原地修改的状态快照。 */
function freezePlan(plan: Plan): Plan {
  return Object.freeze({
    ...plan,
    steps: Object.freeze(plan.steps.map((step) => Object.freeze({
      ...step,
      successCriteria: Object.freeze([...step.successCriteria]),
      dependsOn: Object.freeze([...step.dependsOn]),
      completionEvidenceIds: Object.freeze([...step.completionEvidenceIds]),
    }))),
    evidence: Object.freeze(plan.evidence.map((item) => Object.freeze({
      ...item,
      sourceToolCallIds: Object.freeze([...item.sourceToolCallIds]),
    }))),
    revisions: Object.freeze(plan.revisions.map((item) => Object.freeze({
      ...item,
      evidenceIds: Object.freeze([...item.evidenceIds]),
      replacedStepIds: Object.freeze([...item.replacedStepIds]),
      addedStepIds: Object.freeze([...item.addedStepIds]),
    }))),
  });
}
