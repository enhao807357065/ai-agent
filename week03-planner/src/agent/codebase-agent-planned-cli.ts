import { contentText } from "@earendil-works/pi-ai";
import { createGatewayModelRuntime } from "../model/gateway-models.js";
import { PlanStore, StepKind, StepKindVerifier } from "../planning/plan-store.js";
import { ToolRuntime } from "../runtime/tool-runtime.js";
import { installSigintCancellation } from "./cli-cancellation.js";
import { runCodebaseAgent } from "./codebase-agent-run.js";

/**
 * 显式的教学 Planner 入口。
 *
 * 计划是否启用由 CLI 的服务端装配决定，而不是由 Prompt 文字隐式打开：这里先创建
 * 一个可信 PlanStore 与初始 Plan，再把同一 Store 同时注入 Runtime 和 Run Hook。
 * 因此 create/get/update/revise_plan、[PLAN_STATE]、planStepId 守卫和 Evidence 都观察
 * 同一个计划聚合，而不会出现 Runtime 与 Run 各自持有一份内存计划的分裂状态。
 */
const planId = "fix-session-expiry";
const planStore = new PlanStore("cli-in-memory-current", new StepKindVerifier());
planStore.create(planId, [
  {
    id: "diagnose-expiry",
    kind: StepKind.INVESTIGATION,
    title: "确认 access token 的签发、解析和鉴权链路中缺少会话过期校验",
  },
  {
    id: "design-expiry-boundary",
    kind: StepKind.DESIGN,
    title: "定义 expiresAt 和 [issuedAt, expiresAt) 的边界：now === expiresAt 必须拒绝",
    dependsOn: ["diagnose-expiry"],
  },
  {
    id: "implement-expiry",
    kind: StepKind.IMPLEMENTATION,
    title: "实现 access token 的过期字段、解析校验及鉴权拒绝逻辑",
    dependsOn: ["design-expiry-boundary"],
  },
  {
    id: "verify-expiry-boundary",
    kind: StepKind.VERIFICATION,
    title: "运行 token 临界前、临界点、临界后测试，并验证构建通过",
    dependsOn: ["implement-expiry"],
  },
]);

const runtime = new ToolRuntime({ planStore });
const modelRuntime = createGatewayModelRuntime();
const controller = new AbortController();
const removeSigintCancellation = installSigintCancellation(controller);

console.log(`[agent] planned-mode planId=${planId} version=1 steps=4`);
console.log("[agent] planned-mode hint=use get_plan/start/complete; bind modify_code to implement-expiry and run_tests to verify-expiry-boundary");

const result = await runCodebaseAgent({
  model: modelRuntime.model,
  streamFn: (_model, context, options) => modelRuntime.stream(context, options),
  signal: controller.signal,
  runtime,
  planStore,
  planId,
  maxTurns: 16,
  completion: { kind: "fix", requiredPlanId: planId },
  systemPrompt: [
    "你是受服务端 PlanStore 治理的代码修复 Agent。严格按 [PLAN_STATE] 的 DAG 推进，不得跳过步骤。",
    "investigation/design 步骤用真实 note evidence 完成；implementation 仅在 running 后调用 modify_code 并保留 change evidence；verification 仅在 running 后调用 run_tests，必须运行 test 和 build。",
    "每个步骤完成前调用 update_plan_step(action=complete) 并引用该步骤证据 IDs。所有步骤 completed 前不得宣称修复完成。",
    "modify_code 必须先 read_file，oldText 必须是唯一精确片段；run_tests 仅使用 test 或 build。",
    "最终仅报告真实修改与真实测试结果。",
  ].join("\n"),
  userTask: "修复 login_demo 的 access token 会话过期边界：有效区间为 [issuedAt, expiresAt)，now === expiresAt 必须拒绝。使用当前已绑定的 fix-session-expiry 计划完成诊断、设计、实现、测试和构建验证。",
  onTextDelta: (text) => process.stdout.write(text),
  onLog: (message) => console.log(`[agent] ${message}`),
}).finally(removeSigintCancellation);

process.stdout.write("\n\n");
console.log(JSON.stringify({
  runId: result.state.runId,
  turns: result.state.turnCount,
  stopReason: result.state.stopReason,
  plan: planStore.get(planId),
  readEvidence: result.state.stopCenter.readSourceFiles,
  artifactEvidence: result.state.stopCenter.writtenArtifacts,
  trace: result.trace,
}, null, 2));

const finalAssistant = [...result.messages].reverse().find((message) => message.role === "assistant");
if (finalAssistant?.role === "assistant") {
  const visibleText = contentText(finalAssistant.content);
  if (visibleText.length > 0) console.log(`\nFinal:\n${visibleText}`);
}
