import { existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("十件仓库工具的执行边界", () => {
  it("../../outside.txt 在 Runtime 的 Sandbox Policy 层拒绝，文件 handler 不会触达外部路径", async () => {
    const harness = createWorkspaceHarness();
    const traceId = "trace-outside-txt";
    const result = await harness.invoke("read_file", { path: "../../outside.txt" }, { traceId });

    expect(result).toMatchObject({ ok: false, code: "PATH_OUT_OF_WORKSPACE" });
    expect(harness.store.listAudit(traceId).map((row) => `${row.event_type}:${row.code}`)).toEqual([
      "policy_decision:PATH_OUT_OF_WORKSPACE",
    ]);
  });

  it("run_command 超时会终止进程，并在受控错误中保留 exit_code、signal 与 timed_out", async () => {
    const harness = createWorkspaceHarness({ commandTimeoutMs: 60 });
    const marker = join(harness.workspace, "should-not-exist.txt");
    const pending = await harness.invoke("run_command", {
      command: "sleep 30; touch should-not-exist.txt",
    });
    harness.store.decideApproval(pending.approvalId!, "approve", "manager-1");

    const result = await harness.runtime.executeApproval(pending.approvalId!);

    expect(result).toMatchObject({ ok: false, code: "COMMAND_TIMEOUT" });
    expect(String(result.content)).toContain("timed_out=true");
    expect(String(result.content)).toContain("exit_code=");
    expect(String(result.content)).toContain("signal=");
    expect(existsSync(marker)).toBe(false);
  });
});
