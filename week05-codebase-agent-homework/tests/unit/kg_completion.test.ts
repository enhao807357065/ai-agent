import { describe, expect, it } from "vitest";
import { runManagedAgent } from "../../src/pi/agent-runner.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("结束状态与交付验收", () => {
  it("模型自然结束但没有声明交付物时，执行结束不等于验收完成", async () => {
    const harness = createWorkspaceHarness({ task: "understand" });
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      script: [{ text: "我已分析完毕。" }],
    });

    expect(result.execution.status).toBe("ended");
    expect(result.acceptance.status).toBe("failed");
    expect(result.stop).toMatchObject({ code: "INCOMPLETE_EVIDENCE", stop: true });
    expect(result.acceptance.missing).toContain("交付物 artifacts/login-flow.md 没有成功写入证据");
  });

  it("预算耗尽时保留预算停止原因，未完成任务不被记为完成", async () => {
    const harness = createWorkspaceHarness({ task: "understand" });
    harness.config.runtime.maxTurns = 1;
    const result = await runManagedAgent(harness.platform, {
      prompt: "整理登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      script: [
        { tool_calls: [{ name: "read_file", args: { path: "README.md" } }] },
        { text: "本不应进入第二轮。" },
      ],
    });

    expect(result.stop).toMatchObject({ code: "MAX_TURNS", stop: true });
    expect(result.execution.status).toBe("budget_exhausted");
    expect(result.acceptance.status).toBe("failed");
    expect(result.acceptance.missing).toContain("交付物 artifacts/login-flow.md 没有成功写入证据");
  });
});
