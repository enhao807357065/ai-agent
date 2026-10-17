import type { PlanStore } from "../planning/plan-store.js";

/**
 * CLI 只把服务端已有的受控事件转换为适合学习观察的中文摘要；不打印原始参数、工具结果正文、
 * callId 或隐藏推理。模型可见 text 另以“Agent 说明”显示，避免把它误称为内部思考。
 */
export function createCliObserver(store: PlanStore, planId: string | (() => string | undefined)): { onLog(message: string): void; onTextDelta(text: string): void; flushText(): void } {
  const activePlanId = (): string | undefined => typeof planId === "function" ? planId() : planId;
  let visibleText = "";
  return {
    onLog(message) {
      flushVisibleText();
      const event = message.replace(/^\[run:[^\]]+\]\s*/, "");
      if (event.startsWith("run.started")) return;
      if (event.startsWith("turn.started")) { console.log(`\n── 第 ${valueAfter(event, "number=")} 轮 ──`); return; }
      if (event.startsWith("plan.created")) { printPlan("LLM 创建的执行计划", true); return; }
      if (event.startsWith("plan.snapshot.initial")) { printPlan("初始计划"); return; }
      if (event.startsWith("plan.snapshot.refresh")) { printPlan("计划状态更新"); return; }
      if (event.startsWith("plan.changed")) {
        const operation = valueAfter(event, "operation=");
        const action = valueAfter(event, "action=");
        const stepId = valueAfter(event, "stepId=");
        const description = operation === "revise_plan"
          ? "扩展/调整后续步骤"
          : action === "start" ? `开始步骤 ${stepId}`
            : action === "complete" ? `完成步骤 ${stepId}`
              : action === "fail" ? `标记失败 ${stepId}`
                : action === "block" ? `标记阻塞 ${stepId}`
                  : action === "skip" ? `跳过步骤 ${stepId}` : "步骤状态已推进";
        console.log(`计划已变更：${description}`);
        printPlan("当前计划");
        return;
      }
      if (event.startsWith("tool.executing")) { console.log(`执行工具：${valueAfter(event, "name=")}`); return; }
      if (event.startsWith("tool.finished")) {
        const name = valueAfter(event, "name=");
        console.log(event.includes("isError=true") ? `工具失败：${name}` : `工具完成：${name}`);
        return;
      }
      if (event.startsWith("plan.step.bound")) {
        console.log(`步骤执行中：${valueAfter(event, "stepId=")}（${valueAfter(event, "kind=")}）`);
        return;
      }
      if (event.startsWith("plan.evidence.recorded")) {
        console.log(`证据已记录：${valueAfter(event, "kind=")} → ${valueAfter(event, "stepId=")}`);
        return;
      }
      if (event.startsWith("tool.failed")) {
        const code = valueAfter(event, "code=");
        console.log(`受控拒绝/失败：${valueAfter(event, "name=")}${code ? `（${code}）` : ""}`);
        return;
      }
      if (event.startsWith("loop.stop")) { console.log(`运行停止：${valueAfter(event, "reason=")}`); return; }
      if (event.startsWith("run.finished")) { console.log(`运行结束：${valueAfter(event, "stopReason=")}`); }
    },
    onTextDelta(text) { visibleText += text; },
    flushText() { flushVisibleText(); },
  };

  function flushVisibleText(): void {
    if (visibleText.trim().length > 0) console.log(`Agent 说明：${visibleText.trim()}`);
    visibleText = "";
  }
  function printPlan(title: string, includeDetails = false): void {
    const currentPlanId = activePlanId();
    if (!currentPlanId) return;
    const plan = store.get(currentPlanId);
    console.log(`\n${title}（v${plan.version}）`);
    if (includeDetails) {
      console.log(`目标：${plan.goal}`);
      if (plan.constraints.length > 0) console.log(`约束：${plan.constraints.join("；")}`);
      console.log("任务 DAG：");
    }
    for (const step of plan.steps) {
      if (!includeDetails) { console.log(`  [${step.status}] ${step.id}：${step.title}`); continue; }
      console.log(`  [${step.status}] ${step.id}（${step.kind}）：${step.title}`);
      console.log(`    依赖：${step.dependsOn.length > 0 ? step.dependsOn.join(", ") : "无"}`);
      console.log(`    验收：${step.successCriteria.join("；") || "未声明"}`);
    }
  }
}
function valueAfter(message: string, key: string): string {
  const value = message.slice(message.indexOf(key) + key.length).split(" ")[0] ?? "";
  return value;
}
