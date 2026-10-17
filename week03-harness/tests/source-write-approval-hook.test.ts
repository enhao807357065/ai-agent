import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SourceWriteApprovalGate } from "../src/approval/source-write-approval.js";
import { SourceWriteApprovalHook, expectedSourceWritePostImageDigest, sourceWriteTargetDigest } from "../src/approval/source-write-approval-hook.js";
import { ToolName } from "../src/runtime/tool-runtime.js";

function context(id: string, args: Record<string, unknown>) { return { toolCall: { id, name: ToolName.MODIFY_CODE, arguments: args }, args } as never; }
describe("SourceWriteApprovalHook", () => {
  it("blocks source mutation before approval, allows only its exact approved call, and re-blocks after target changes", async () => {
    const root = await (await import("node:fs/promises")).mkdtemp("/tmp/week03-approval-"); await mkdir(join(root, "src")); await writeFile(join(root, "src", "token.ts"), "const expired = false;\n");
    const gate = new SourceWriteApprovalGate(); const hook = new SourceWriteApprovalHook(gate, root); const args = { path: "src/token.ts", oldText: "false", newText: "true" };
    expect(await hook.beforeToolCall(context("write-1", args))).toMatchObject({ block: true, terminate: true, reason: expect.stringContaining("APPROVAL_REQUIRED") });
    hook.approve("write-1"); expect(await hook.beforeToolCall(context("write-1", args))).toBeUndefined();
    await writeFile(join(root, "src", "token.ts"), "const expired = false; // concurrent change\n");
    expect(await hook.beforeToolCall(context("write-1", args))).toMatchObject({ block: true, reason: expect.stringContaining("APPROVAL_REQUIRED") });
    await (await import("node:fs/promises")).rm(root, { recursive: true });
  });
  it("derives and verifies the exact approved post-image", async () => {
    const root = await (await import("node:fs/promises")).mkdtemp("/tmp/week03-approval-"); await mkdir(join(root, "src")); const file = join(root, "src", "token.ts"); await writeFile(file, "const expired = false;\n");
    const proposal = { toolName: "modify_code" as const, parameters: { path: "src/token.ts", oldText: "false", newText: "true" } };
    const expected = await expectedSourceWritePostImageDigest(root, proposal);
    await writeFile(file, "const expired = true;\n");
    expect(await sourceWriteTargetDigest(root, proposal)).toBe(expected);
    await (await import("node:fs/promises")).rm(root, { recursive: true });
  });
  it("hashes a canonical patch intent independently from the action ID and target pre-image", async () => {
    const { patchHashOf } = await import("../src/approval/source-write-approval.js");
    const one = { actionId: "one", toolName: "modify_code" as const, parameters: { path: "src/token.ts", oldText: "false", newText: "true" }, targetDigest: "before-a" };
    expect(patchHashOf(one)).toBe(patchHashOf({ ...one, actionId: "two", targetDigest: "before-b" }));
    expect(patchHashOf(one)).not.toBe(patchHashOf({ ...one, parameters: { ...one.parameters, newText: "strict" } }));
  });
  it("rejects an approval without ever allowing handler execution", async () => {
    const root = await (await import("node:fs/promises")).mkdtemp("/tmp/week03-approval-"); await mkdir(join(root, "src")); await writeFile(join(root, "src", "token.ts"), "x"); const hook = new SourceWriteApprovalHook(new SourceWriteApprovalGate(), root);
    expect(await hook.beforeToolCall(context("write-2", { path: "src/token.ts", oldText: "x", newText: "y" }))).toMatchObject({ block: true }); hook.reject("write-2"); expect(await hook.beforeToolCall(context("write-2", { path: "src/token.ts", oldText: "x", newText: "y" }))).toMatchObject({ block: true }); expect(await readFile(join(root, "src", "token.ts"), "utf8")).toBe("x"); await (await import("node:fs/promises")).rm(root, { recursive: true });
  });
  it("auto-approve still records an exact frozen proposal and allows only that unchanged action", async () => {
    const root = await (await import("node:fs/promises")).mkdtemp("/tmp/week03-approval-"); await mkdir(join(root, "src")); await writeFile(join(root, "src", "token.ts"), "x"); const gate = new SourceWriteApprovalGate(); const hook = new SourceWriteApprovalHook(gate, root, { autoApprove: true });
    expect(await hook.beforeToolCall(context("write-3", { path: "src/token.ts", oldText: "x", newText: "y" }))).toBeUndefined(); expect(gate.decisions()).toHaveLength(1);
    await (await import("node:fs/promises")).rm(root, { recursive: true });
  });
});
