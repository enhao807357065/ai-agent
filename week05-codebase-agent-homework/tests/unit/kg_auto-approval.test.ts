import { randomUUID } from "node:crypto";
import { describe, expect, it } from "vitest";
import { createWorkspaceHarness } from "./helpers/workspace.js";

/**
 * 自动审批的边界：它只替换“谁来答这张单”，不替换审批本身。
 * 单子照开、参数指纹照算、审计照记、动作仍是一次性领取；策略 deny 不归它管。
 * 真实 Agent Loop 先过 gateForPi 再调工具，所以这里照那条顺序打。
 */
const args = { path: "src/auth/note.md", content: "# 说明\n" };

async function gateThenInvoke(
  harness: ReturnType<typeof createWorkspaceHarness>,
  modelName: string,
  toolArgs: Record<string, unknown>,
) {
  const request = { context: harness.context(), toolCallId: randomUUID(), modelName, args: toolArgs };
  const gate = await harness.runtime.gateForPi(request);
  const result = gate.block ? undefined : await harness.runtime.invoke(request);
  return { gate, result };
}

describe("自动审批", () => {
  it("不给身份时保持原行为：挂起待批，文件不落盘", async () => {
    const harness = createWorkspaceHarness();
    const pending = await harness.invoke("write_file", args);
    expect(pending.action).toBe("confirm");
    expect(pending.code).toBe("APPROVAL_REQUIRED");
    expect(pending.approvalId).toBeTruthy();
    expect(harness.store.getApproval(pending.approvalId!)?.status).toBe("pending");
    expect(() => harness.read("src/auth/note.md")).toThrow();
  });

  it("给了身份就当场批准并执行一次，单子与审计都留痕", async () => {
    const harness = createWorkspaceHarness({ autoApprove: { actor: "auto-approver" } });
    const { gate, result } = await gateThenInvoke(harness, "write_file", args);
    expect(gate.block).toBe(false);
    expect(result?.ok).toBe(true);
    // 动作真的执行了，不是只把单子批了。
    expect(harness.read("src/auth/note.md")).toBe("# 说明\n");

    const approvals = harness.store.listApprovals();
    expect(approvals).toHaveLength(1);
    expect(approvals[0].status).toBe("consumed");
    expect(approvals[0].decided_by).toBe("auto-approver");

    const audit = harness.store.listAudit();
    const auto = audit.filter((row) => row.code === "AUTO_APPROVED");
    expect(auto).toHaveLength(1);
    expect(auto[0].status).toBe("allow");

    // 同一张单不能再领第二次：不给动作，也不重复写文件。
    const replay = await harness.runtime.executeApproval(approvals[0].id, args);
    expect(replay.ok).toBe(false);
    expect(replay.code).toBe("APPROVAL_NOT_EXECUTABLE");
  });

  it("策略 deny 不受自动审批影响：不开单、不放行", async () => {
    const harness = createWorkspaceHarness({ task: "fix_tests", autoApprove: { actor: "auto-approver" } });
    const { gate, result } = await gateThenInvoke(harness, "write_file", {
      path: "tests/session-boundary.test.ts",
      content: "改松断言\n",
    });
    // 硬拒绝在门口就被拦下，根本轮不到审批，更轮不到自动签字。
    expect(result).toBeUndefined();
    expect(gate.block).toBe(true);
    expect(gate.code).toBe("PATH_DENY_WRITE");
    expect(harness.store.listApprovals()).toHaveLength(0);
  });

  it("自动审批不会把同一批动作批两次：重复 gate 也只开一张单", async () => {
    const harness = createWorkspaceHarness({ autoApprove: { actor: "auto-approver" } });
    await gateThenInvoke(harness, "write_file", args);
    await gateThenInvoke(harness, "write_file", { ...args, content: "# 第二遍\n" });
    // 两次独立调用各开一张单，各自消费一次；不会出现“一张单执行两遍”。
    const approvals = harness.store.listApprovals();
    expect(approvals).toHaveLength(2);
    expect(approvals.every((row) => row.status === "consumed")).toBe(true);
    expect(harness.read("src/auth/note.md")).toBe("# 第二遍\n");
  });
});
