import { describe, expect, it } from "vitest";
import { runManagedAgent } from "../../src/pi/agent-runner.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("turn 级工具使用日志", () => {
  it("每个 turn 输出本轮工具名及调用次数；无工具 turn 明确为 0 次", async () => {
    const harness = createWorkspaceHarness();
    harness.write("src/login.ts", "export const login = true;\n");
    const logs: string[] = [];

    await runManagedAgent(harness.platform, {
      prompt: "读取登录逻辑",
      userId: "trainer-1",
      tenantId: "tenant_a",
      roles: ["maintainer"],
      script: [
        {
          tool_calls: [
            { name: "read_file", args: { path: "src/login.ts" } },
            { name: "read_file", args: { path: "src/login.ts" } },
            { name: "list_files", args: { pattern: "src/**/*.ts" } },
          ],
        },
        { text: "读取结束。" },
      ],
      onTurnTools: (line) => logs.push(line),
    });

    expect(logs).toEqual([
      "[本轮工具 · 第1轮] 共 3 次 · read_file × 2，list_files × 1",
      "[本轮工具 · 第2轮] 共 0 次 · 无",
    ]);
  });
});
