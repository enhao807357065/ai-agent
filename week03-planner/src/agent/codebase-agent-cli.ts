import { contentText } from "@earendil-works/pi-ai";
import { createGatewayModelRuntime } from "../model/gateway-models.js";
import { CodeVersionTracker } from "../planning/code-version.js";
import { PlanStore, StepKind, StepKindVerifier } from "../planning/plan-store.js";
import { ToolRuntime } from "../runtime/tool-runtime.js";
import { installSigintCancellation } from "./cli-cancellation.js";
import { runCodebaseAgent, codebaseAgentSystemPrompt } from "./codebase-agent-run.js";
import { readTaskInput } from "./task-input.js";

/**
 * 正式入口使用 Evidence-first 动态计划，而不是把“必须改代码”写死为 mock DAG。
 * 初始图只包含可验证的诊断；Agent 完成诊断后必须用 revise_plan 基于真实 Evidence
 * 追加“实现+验证”或“仅验证”分支。PlanStore 决定 revision 是否可发布。
 */
const defaultTask = "审计 login_demo 的 access token expiry 边界：[issuedAt, expiresAt)，now === expiresAt 必须拒绝。根据诊断事实动态扩展计划；若现有实现已满足要求，不得制造代码修改，改为执行真实测试和构建验证。";
const task = readTaskInput(process.argv.slice(2), process.env.CODEBASE_AGENT_TASK, defaultTask);
const planId = `dynamic-${crypto.randomUUID()}`;
const codeVersion = new CodeVersionTracker();
const planStore = new PlanStore(() => codeVersion.current(), new StepKindVerifier());
planStore.create(planId, [{
  id: "diagnose-task",
  kind: StepKind.INVESTIGATION,
  title: "根据用户任务检查相关源码、配置与现有测试，确认根因、范围和可验证交付路径",
  successCriteria: ["引用真实 read_file 或 search_code 调用的 note Evidence", "明确后续需要实施还是仅验证"],
}], {
  goal: task.value,
  constraints: ["先诊断再扩展 Plan", "不得伪造变更、测试结果或 Evidence", "只使用服务端允许的受控工具"],
});
const runtime = new ToolRuntime({ planStore, codeVersionTracker: codeVersion });
const modelRuntime = createGatewayModelRuntime();
const controller = new AbortController();
const removeSigintCancellation = installSigintCancellation(controller);

console.log(`[agent] dynamic-plan mode=official planId=${planId} version=1 seedSteps=1 taskSource=${task.source}`);
const result = await runCodebaseAgent({
  model: modelRuntime.model,
  streamFn: (_model, context, options) => modelRuntime.stream(context, options),
  signal: controller.signal,
  runtime,
  planStore,
  planId,
  maxTurns: 20,
  completion: { kind: "fix", requiredPlanId: planId },
  systemPrompt: [
    codebaseAgentSystemPrompt,
    "本次正式入口使用 Evidence-first 动态计划：初始只有 diagnose-task，不预设缺陷存在。诊断完成后必须用 revise_plan 基于真实 Evidence additions 追加实现+验证或仅验证分支。",
  ].join("\n"),
  userTask: task.value,
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
