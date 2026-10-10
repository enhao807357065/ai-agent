import { describe, expect, it, vi } from "vitest";
import { createCliObserver } from "../src/agent/cli-observer.js";

describe("CLI observer", () => {
  it("prints Chinese plan and governed lifecycle summaries without call IDs or raw tool payloads", () => {
    const store = { get: () => ({ version: 2, steps: [{ id: "diagnose", status: "completed", title: "诊断" }, { id: "verify", status: "pending", title: "验证" }] }) } as never;
    const observer = createCliObserver(store, "plan"); const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    observer.onLog("[run:r-1] plan.snapshot.initial planId=plan version=2 callId=secret");
    observer.onLog("[run:r-1] tool.executing name=read_file callId=secret");
    observer.onLog("[run:r-1] plan.evidence.recorded id=e-1 stepId=diagnose kind=note codeVersion=v1");
    observer.onLog("[run:r-1] plan.changed operation=revise_plan version=2 steps=[diagnose:completed,fix:pending] revisions=1");
    observer.onLog("[run:r-1] plan.changed operation=update_plan_step action=complete stepId=diagnose version=2 steps=[diagnose:completed,fix:pending] revisions=1");
    observer.onTextDelta("我先检查会话策略。"); observer.flushText();
    const output = log.mock.calls.flat().join("\n");
    expect(output).toContain("初始计划（v2）"); expect(output).toContain("[completed] diagnose：诊断"); expect(output).toContain("执行工具：read_file"); expect(output).toContain("证据已记录：note → diagnose"); expect(output).toContain("计划已变更：扩展/调整后续步骤"); expect(output).toContain("计划已变更：完成步骤 diagnose"); expect(output).toContain("Agent 说明：我先检查会话策略。"); expect(output).not.toContain("secret");
    log.mockRestore();
  });
});
