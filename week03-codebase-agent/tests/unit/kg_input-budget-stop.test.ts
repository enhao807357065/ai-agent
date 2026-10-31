import { describe, expect, it } from "vitest";
import { runManagedAgent } from "../../src/pi/agent-runner.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("请求前整体输入预算", () => {
  it("超过输入预算时不调用离线决策器，并写入可查询的停止原因", async () => {
    const harness = createWorkspaceHarness();
    harness.config.context.maxInputTokens = 1;
    let selected = 0;

    const result = await runManagedAgent(harness.platform, {
      prompt: "读取登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      script: [{ text: "这一句不应由模型生成。" }],
      onDeciderStep: () => { selected += 1; },
    });

    expect(selected).toBe(0);
    expect(result.stop).toMatchObject({ stop: true, code: "INPUT_BUDGET_EXCEEDED" });
    expect(result.final_answer).toBe("");
    expect(harness.store.getTrace(result.trace_id).some((row) => row.event_type === "input_budget.exceeded")).toBe(true);
  });
});
