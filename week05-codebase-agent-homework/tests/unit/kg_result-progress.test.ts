import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { renderContext, type RenderableMessage } from "../../src/context-budget.js";
import { createWorkspaceHarness } from "./helpers/workspace.js";

describe("结果持久化与下一轮观察", () => {
  it("长结果审计只保存按行截断摘要与 full_sha256；模型观察带 truncated 标记和产物引用", async () => {
    const harness = createWorkspaceHarness();
    harness.write("src/big.ts", Array.from({ length: 1_000 }, (_, index) => `log-${index + 1}-${"x".repeat(20)}`).join("\n"));
    const traceId = "trace-long-observation";

    const result = await harness.invoke("read_file", { path: "src/big.ts" }, { traceId });

    expect(String(result.content)).toContain("[truncated]");
    expect(String(result.content)).toContain("artifact=");
    const audit = harness.store.listAudit(traceId)[0];
    const stored = JSON.parse(String(audit.output_json));
    expect(stored.truncated).toBe(true);
    expect(stored.full_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(stored.text).not.toContain("log-80");
    expect(readFileSync(String(stored.artifact_path), "utf8")).toContain("log-80");
  });

  it("第三轮时第一步正文被移出，只在摘要保留调用、失败状态与 toolCallId 证据引用", () => {
    const step = (index: number, error = false): RenderableMessage[] => [
      { role: "assistant", content: [{ type: "toolCall", id: `call-${index}`, name: "read_file", arguments: { path: `src/${index}.ts` } }] },
      { role: "toolResult", toolCallId: `call-${index}`, toolName: "read_file", isError: error, code: error ? "NOT_FOUND" : "OK", content: [{ type: "text", text: `first-step-body-${index}\n${"more-body\n".repeat(80)}` }] },
    ];
    const source: RenderableMessage[] = [
      { role: "system", content: "rules" },
      { role: "user", content: "goal" },
      ...step(1, true),
      ...step(2),
      ...step(3),
    ];
    const rendered = renderContext(source, { keepRecentTurns: 2, auditMaxLines: 30, auditMaxChars: 4_000, maxInputTokens: 12_000 });
    const summary = String(rendered.messages[2].content);

    expect(rendered.stats.steps_summarized).toBe(1);
    expect(summary).toContain("read_file(path=src/1.ts) → NOT_FOUND");
    expect(summary).toContain("evidence=toolCallId:call-1");
    expect(rendered.messages.map((message) => JSON.stringify(message.content)).join("\n")).not.toContain("first-step-body-1");
  });
});
