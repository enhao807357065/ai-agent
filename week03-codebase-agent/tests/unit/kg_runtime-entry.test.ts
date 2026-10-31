import { existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("Runtime 工具调用四道门", () => {
  it("read_file.path 为数字时，在 Runtime 参数校验处拒绝，保留 toolCallId 且 handler 不触达", async () => {
    const harness = createWorkspaceHarness();
    harness.write("src/example.ts", "export const answer = 42;\n");
    const traceId = "trace-invalid-path-number";
    const toolCallId = "call-invalid-path-number";
    let handlerCalls = 0;
    const readTool = harness.registry.get("read_file");
    const original = readTool.handler;
    readTool.handler = async (input) => {
      handlerCalls += 1;
      return original(input);
    };

    const result = await harness.runtime.invoke({
      context: harness.context({ traceId }),
      toolCallId,
      modelName: readTool.modelName,
      args: { path: 42 as unknown as string },
    });

    expect(result).toMatchObject({ ok: false, action: "deny", code: "INVALID_ARGUMENT" });
    expect(handlerCalls).toBe(0);
    const denied = harness.store.getTrace(traceId).find((row) => row.event_type === "tool.denied");
    expect(String(denied?.payload_json)).toContain(toolCallId);
  });

  it("待审批写入冻结参数；未批准不写，批准后只能执行这一次获批操作", async () => {
    const harness = createWorkspaceHarness();
    const target = join(harness.workspace, "docs", "approved.md");
    const args = { path: "docs/approved.md", content: "approved content\n" };
    const pending = await harness.invoke("write_file", args);

    expect(pending).toMatchObject({ ok: false, action: "confirm", code: "APPROVAL_REQUIRED" });
    expect(existsSync(target)).toBe(false);
    expect(pending.approvalId).toBeTruthy();

    harness.store.decideApproval(pending.approvalId!, "approve", "manager-1");
    const [first, duplicate] = await Promise.all([
      harness.runtime.executeApproval(pending.approvalId!),
      harness.runtime.executeApproval(pending.approvalId!),
    ]);

    expect([first.code, duplicate.code].filter((code) => code === "OK")).toHaveLength(1);
    expect([first.code, duplicate.code]).toContain("APPROVAL_NOT_EXECUTABLE");
    expect(harness.read("docs/approved.md")).toBe("approved content\n");
  });
});
