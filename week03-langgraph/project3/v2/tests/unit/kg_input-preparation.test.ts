import { describe, expect, it } from "vitest";
import { assertInputBudget, buildModelInput } from "../../src/context-budget.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";
import { renderSystemPrompt } from "../../src/pi/prompt-client.js";

describe("每轮模型输入装配", () => {
  it("本地 prompts.yaml 改一行后，下一次渲染直接使用新内容，并注入任务简报与交付物", async () => {
    const harness = createWorkspaceHarness({ task: "understand" });
    const first = await renderSystemPrompt(harness.config, harness.context());
    expect(first.prompt).toContain("登录调用链说明");
    expect(first.prompt).toContain("artifacts/login-flow.md");

    const file = harness.config.prompt.file;
    const original = await import("node:fs").then(({ readFileSync }) => readFileSync(file, "utf8"));
    await import("node:fs").then(({ writeFileSync }) => writeFileSync(file, original.replace("你是 {{ agent_name }}", "热更新标记：你是 {{ agent_name }}"), "utf8"));
    const second = await renderSystemPrompt(harness.config, harness.context());
    expect(second.prompt).toContain("热更新标记");
    expect(second.contentHash).not.toBe(first.contentHash);
  });

  it("模型工具定义直接来自 Runtime Registry 中同一批 RegisteredTool", () => {
    const harness = createWorkspaceHarness();
    const input = buildModelInput({
      systemPrompt: "规则",
      history: [{ role: "user", content: "读取文件" }],
      registry: harness.registry,
      context: harness.context(),
      policy: harness.policy,
    });
    const runtimeTool = harness.registry.get("read_file");
    const exposed = input.tools.find((tool) => tool.modelName === runtimeTool.modelName);
    expect(exposed?.metadata.parameters).toBe(runtimeTool.metadata.parameters);
  });

  it("输入超过预算时在请求前拒绝，说明 token 上限与实际值", () => {
    expect(() => assertInputBudget({ inputTokens: 101, maxInputTokens: 100 })).toThrow(
      "INPUT_BUDGET_EXCEEDED: 本轮输入估算 101 tokens，超过上限 100 tokens",
    );
  });
});
