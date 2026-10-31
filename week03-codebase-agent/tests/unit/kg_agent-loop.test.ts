import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { runManagedAgent } from "../../src/pi/agent-runner.js";
import { CodeVersionTracker } from "../../src/planning/code-version.js";
import { EvidenceKind, PlanStore, StepKind } from "../../src/planning/plan-store.js";
import type { ScriptedStep } from "../../src/pi/decider.js";
import { createWorkspaceHarness, type WorkspaceHarness } from "./helpers/workspace.js";

function harnessWithLogin(): WorkspaceHarness {
  const harness = createWorkspaceHarness();
  harness.write("src/login.ts", "export function login(email: string) { return email.length > 0; }\n");
  harness.write("src/server.ts", "import { login } from './login';\nconsole.log(login);\n");
  harness.write(".env", "DEEPSEEK_API_KEY=should-never-be-read\n");
  return harness;
}

function run(harness: WorkspaceHarness, script: ScriptedStep[]) {
  return runManagedAgent(harness.platform, {
    prompt: "把登录逻辑整理清楚",
    userId: "trainer-1",
    tenantId: "tenant_a",
    roles: ["maintainer"],
    script,
  });
}

function auditCodes(harness: WorkspaceHarness, traceId: string): string[] {
  return harness.store
    .listAudit(traceId)
    .map((row) => `${String(row.event_type)}:${String(row.tool_name)}:${String(row.code)}`);
}

describe("离线 Agent Loop：脚本决策器 + 十件工具 + 治理", () => {
  it("按脚本读代码并收尾，全程不联网", async () => {
    const harness = harnessWithLogin();
    const result = await run(harness, [
      { text: "先定位登录入口。", tool_calls: [{ name: "search_code", args: { pattern: "login", path: "src" } }] },
      { tool_calls: [{ name: "read_file", args: { path: "src/login.ts" } }] },
      { text: "login.ts 里只是一个长度判断，没有真正的鉴权。" },
    ]);

    expect(result.decider.kind).toBe("scripted");
    expect(result.decider.requested).toBe("auto");
    expect(result.decider.degraded).toBe(false);
    expect(result.decider.steps).toBe(3);
    expect(result.prompt_source).toBe("local");
    expect(result.final_answer).toContain("没有真正的鉴权");
    expect(result.stop.code).toBe("COMPLETED");
    expect(result.budget.turns).toBe(3);
    expect(result.budget.tool_calls).toBe(2);
    expect(auditCodes(harness, result.trace_id)).toEqual(
      expect.arrayContaining(["tool_execution:search_code:OK", "tool_execution:read_file:OK"]),
    );
  });

  it("faux 也按真实规则算 Token：预算在没有网关时依然有效", async () => {
    const harness = harnessWithLogin();
    const small = await run(harness, [{ text: "结束。" }]);
    expect(small.budget.total_tokens).toBeGreaterThan(0);

    const big = await run(harness, [
      { tool_calls: [{ name: "read_file", args: { path: "src/login.ts" } }] },
      { tool_calls: [{ name: "read_file", args: { path: "src/server.ts" } }] },
      { text: "读完了。" },
    ]);
    expect(big.budget.total_tokens).toBeGreaterThan(small.budget.total_tokens);
    expect(big.budget.cost_usd).toBeGreaterThanOrEqual(0);
  });

  it("写文件被拦在审批上：Loop 停下、文件不落盘，审批后才执行", async () => {
    const harness = harnessWithLogin();
    const target = join(harness.workspace, "artifacts", "login-flow.md");
    const result = await run(harness, [
      { text: "直接产出文档。", tool_calls: [{ name: "write_file", args: { path: "artifacts/login-flow.md", content: "# 登录调用链\n" } }] },
    ]);

    expect(result.stop.code).toBe("AWAITING_APPROVAL");
    expect(result.pending_approval_id).toBeTruthy();
    expect(existsSync(target)).toBe(false);

    harness.store.decideApproval(result.pending_approval_id as string, "approve", "trainer-1");
    const executed = await harness.runtime.executeApproval(result.pending_approval_id as string);
    expect(executed.code).toBe("OK");
    expect(harness.read("artifacts/login-flow.md")).toBe("# 登录调用链\n");
  });

  it("越界读 .env：策略在模型之外拦住，密钥不回喂也不落审计", async () => {
    const harness = harnessWithLogin();
    const result = await run(harness, [
      { text: "看看环境变量文件。", tool_calls: [{ name: "read_file", args: { path: ".env" } }] },
      { text: "这条路被拒了，改读源码。" },
    ]);

    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:read_file:PATH_DENY_READ");
    expect(JSON.stringify(harness.store.listAudit(result.trace_id))).not.toContain("should-never-be-read");
    expect(result.final_answer).toContain("被拒");
  });

  it("相同调用重复第三次被 Loop Guard 掐掉，前两次照常执行", async () => {
    const harness = harnessWithLogin();
    const repeat = { name: "list_files", args: { pattern: "**/*.ts", path: "src" } };
    const result = await run(harness, [
      { tool_calls: [repeat] },
      { tool_calls: [repeat] },
      { tool_calls: [repeat] },
      { text: "别再重复了。" },
    ]);

    expect(result.budget.tool_calls).toBe(2);
    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:list_files:REPEATED_TOOL_CALL");
  });

  it("模型给的参数不合契约：pi 先按 Schema 拦下，Runtime 补一条带原因的审计与 Trace", async () => {
    const harness = harnessWithLogin();
    const result = await run(harness, [
      { tool_calls: [{ name: "list_files", args: { path: "src" } }] },
      { text: "少给了 pattern，补上再试。" },
    ]);

    expect(result.budget.tool_calls).toBe(0);
    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:list_files:PI_TOOL_ERROR");
    const denied = harness.store
      .getTrace(result.trace_id)
      .find((row) => row.event_type === "tool.denied");
    expect(String(denied?.payload_json)).toContain("pattern");
  });

  it("plan 模式里写操作一律拦住：同一份脚本，换个模式就有不同结局", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      mode: "plan",
      script: [
        { tool_calls: [{ name: "write_file", args: { path: "artifacts/plan.md", content: "x\n" } }] },
        { text: "只出方案，不改代码。" },
      ],
    });

    expect(result.budget.tool_calls).toBe(1);
    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:write_file:PLAN_MODE_DENIED");
    expect(existsSync(join(harness.workspace, "artifacts", "plan.md"))).toBe(false);
  });

  it("--plan 初始没有活动计划，并以 PLAN_REQUIRED 阻止仓库工具", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "read_file", args: {} }] },
        { text: "请先创建计划。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.activePlanId).toBeUndefined();
    expect(durable.plan).toBeUndefined();
    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:read_file:PLAN_REQUIRED");
  });

  it("--plan 在 create_plan 与 start step 后才允许仓库工具，并写入服务端证据", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "整理登录逻辑",
          steps: [{ id: "inspect", title: "阅读登录实现", kind: "investigation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "inspect" } }] },
        { tool_calls: [{ name: "read_file", args: { path: "src/login.ts" } }] },
        { text: "已阅读。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.activePlanId).toBe(`plan:${result.run_id}`);
    expect(auditCodes(harness, result.trace_id)).toContain("tool_execution:read_file:OK");
    expect(durable.plan.steps[0]).toMatchObject({ status: "running" });
    expect(durable.plan.evidence).toEqual(expect.arrayContaining([
      expect.objectContaining({ stepId: "inspect", kind: "note", toolCallId: expect.any(String) }),
    ]));
  });

  it("--plan 的输入预算和 Trace 计入四个附加计划工具", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "先创建计划",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [{ text: "暂不调用工具。" }],
    });

    const prepared = harness.store.getTrace(result.trace_id).find((row) => row.event_type === "input.prepared");
    expect(JSON.parse(String(prepared?.payload_json)).tool_count).toBe(harness.registry.snapshot(harness.context(), harness.policy).length + 4);
  });

  it("--plan 只接受服务端从受治理工具生成的证据", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "整理登录逻辑",
          steps: [{ id: "inspect", title: "阅读登录实现", kind: "investigation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "inspect" } }] },
        { tool_calls: [{ name: "read_file", args: { path: "src/login.ts" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "record_evidence", step_id: "inspect" } }] },
        { tool_calls: [{ name: "update_plan_step", args: {
          action: "complete",
          step_id: "inspect",
        } }] },
        { text: "证据由服务端根据受治理工具调用生成。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps[0]).toMatchObject({ status: "completed", completionEvidenceIds: [expect.any(String)] });
    expect(durable.plan.evidence).toHaveLength(1);
    expect(durable.plan.evidence[0]).toMatchObject({ kind: "note", stepId: "inspect", toolCallId: expect.any(String) });
    expect(durable.plan.evidence).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "forged" }),
    ]));
  });
  it("--plan 把计划创建、步骤变更与服务端证据写入 Trace", async () => {
    const harness = createWorkspaceHarness({ task: "debt_audit" });
    harness.write("src/debt.ts", "export const debt = 'known';\n");
    const result = await runManagedAgent(harness.platform, {
      prompt: "先调查，再写报告",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "先调查，再写报告",
          steps: [
            { id: "inspect", title: "阅读源码", kind: "investigation" },
            { id: "draft", title: "写入报告", kind: "implementation", depends_on: ["inspect"] },
          ],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "inspect" } }] },
        { tool_calls: [{ name: "read_file", args: { path: "src/debt.ts" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "complete", step_id: "inspect" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "draft" } }] },
        { tool_calls: [{ name: "write_file", args: { path: "docs/report.md", content: "# report\n" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "complete", step_id: "draft" } }] },
        { text: "报告已写入。" },
      ],
    });

    const trace = harness.store.getTrace(result.trace_id);
    expect(trace.map((row) => row.event_type)).toEqual(expect.arrayContaining([
      "plan.created", "plan.step_started", "plan.evidence_recorded", "plan.step_completed",
    ]));
    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps.map((step: { id: string; status: string }) => [step.id, step.status])).toEqual([
      ["inspect", "completed"], ["draft", "running"],
    ]);
    expect(result.plan).toMatchObject({
      id: `plan:${result.run_id}`,
      status: "incomplete",
      steps: [
        { id: "inspect", status: "completed" },
        { id: "draft", status: "running" },
      ],
    });
  });

  it("persists the code revision in a durable plan run", async () => {
    const harness = createWorkspaceHarness({ task: "debt_audit" });
    const result = await runManagedAgent(harness.platform, {
      prompt: "写入报告",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "写入报告", steps: [{ id: "implement", title: "写入报告", kind: "implementation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "implement" } }] },
        { tool_calls: [{ name: "write_file", args: { path: "docs/report.md", content: "# report\n" } }] },
        { text: "写入后暂停。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.codeRevision).toBe(1);
    expect(durable.plan.evidence[0]).toMatchObject({ kind: "change", codeVersion: "run-code-1" });
  });

  it("restores a persisted code revision before recording later evidence", () => {
    const harness = createWorkspaceHarness({ task: "debt_audit" });
    const persistedRevision = 1;
    const resumedVersions = new CodeVersionTracker(persistedRevision);
    const planStore = new PlanStore(harness.store, resumedVersions);
    planStore.create("resumed-plan", [{ id: "second", title: "写第二份文档", kind: StepKind.IMPLEMENTATION }]);
    planStore.start("resumed-plan", "second");
    resumedVersions.advanceAfterCodeChange();
    const evidence = planStore.recordToolEvidence("resumed-plan", "second", {
      toolCallId: "resumed-write", kind: EvidenceKind.CHANGE, summary: "write_file: second", workspaceDigest: "digest-2",
    });
    expect(evidence.codeVersion).toBe("run-code-2");
    expect(resumedVersions.revision()).toBe(2);
  });

  it("--plan 使用受治理写入产生的当前版本 change evidence 完成实施步骤", async () => {
    const harness = createWorkspaceHarness({ task: "debt_audit" });
    const result = await runManagedAgent(harness.platform, {
      prompt: "写入报告",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "写入报告",
          steps: [{ id: "implement", title: "写入报告", kind: "implementation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "implement" } }] },
        { tool_calls: [{ name: "write_file", args: { path: "docs/report.md", content: "# report\n" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "complete", step_id: "implement" } }] },
        { text: "报告已写入。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps[0]).toMatchObject({ status: "completed" });
    expect(durable.plan.evidence[0]).toMatchObject({ kind: "change", codeVersion: "run-code-1" });
  });

  it("--plan 从 run_test 结果记录验证 outcome", async () => {
    const harness = createWorkspaceHarness({ testCommand: ["node", "-e", "process.exit(0)"] });
    const result = await runManagedAgent(harness.platform, {
      prompt: "验证实现",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "验证实现",
          steps: [{ id: "verify", title: "运行测试", kind: "verification" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "verify" } }] },
        { tool_calls: [{ name: "run_test", args: {} }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "complete", step_id: "verify" } }] },
        { text: "测试通过。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps[0]).toMatchObject({ status: "completed" });
    expect(durable.plan.verificationOutcomes).toEqual([
      expect.objectContaining({ passed: true, scope: "all", toolCallId: expect.any(String) }),
    ]);
  });

  it("--plan 不接受 record_evidence 作为 update_plan_step action", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "整理登录逻辑",
          steps: [{ id: "inspect", title: "阅读登录实现", kind: "investigation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "start", step_id: "inspect" } }] },
        { tool_calls: [{ name: "read_file", args: { path: "src/login.ts" } }] },
        { tool_calls: [{ name: "update_plan_step", args: { action: "record_evidence", step_id: "inspect" } }] },
        { text: "模型不能直接写入证据。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps[0]).toMatchObject({ status: "running" });
    expect(durable.plan.evidence).toHaveLength(1);
  });

  it("--plan 不接受 update_plan_step 的 evidence 字段", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      durablePlan: true,
      script: [
        { tool_calls: [{ name: "create_plan", args: {
          goal: "整理登录逻辑",
          steps: [{ id: "inspect", title: "阅读登录实现", kind: "investigation" }],
        } }] },
        { tool_calls: [{ name: "update_plan_step", args: {
          action: "start",
          step_id: "inspect",
          evidence: [{ id: "forged", kind: "note", summary: "forged" }],
        } }] },
        { text: "模型不能注入证据字段。" },
      ],
    });

    const durable = JSON.parse(String(harness.store.getRun(result.run_id)!.payload_json));
    expect(durable.plan.steps[0]).toMatchObject({ status: "pending" });
    expect(durable.plan.evidence).toEqual([]);
  });

  it("只给 reviewer 角色时，写操作在 RBAC 这一层就被挡住", async () => {
    const harness = harnessWithLogin();
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "reviewer-1",
      tenantId: "tenant_a",
      roles: ["reviewer"],
      mode: "execute",
      script: [
        { tool_calls: [{ name: "write_file", args: { path: "artifacts/plan.md", content: "x\n" } }] },
        { text: "我没有写权限。" },
      ],
    });

    expect(auditCodes(harness, result.trace_id)).toContain("policy_decision:write_file:RBAC_DENIED");
    expect(existsSync(join(harness.workspace, "artifacts", "plan.md"))).toBe(false);
  });

  it("提示词渲染进 Trace：交付时能说清这次用的是哪个版本、哪个哈希", async () => {
    const harness = harnessWithLogin();
    const result = await run(harness, [{ text: "结束。" }]);
    const rows = harness.store.getTrace(result.trace_id);
    const rendered = rows.find((row) => row.event_type === "prompt_rendered");
    expect(rendered).toBeDefined();
    const payload = JSON.parse(String(rendered?.payload_json));
    expect(payload).toMatchObject({ prompt_name: "codebase.agent", source: "local", version: "v1" });
    expect(readFileSync("config/prompts.yaml", "utf8")).toContain("codebase.agent");
  });
});
