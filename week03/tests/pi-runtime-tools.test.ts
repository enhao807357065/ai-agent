import { describe, expect, it } from "vitest";
import { Check } from "typebox/value";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";
import {
  createPiRuntimeTools,
  RuntimeToolFailure,
} from "../src/tools/pi-runtime-tools.js";
import { ToolName, ToolResultCode, type ToolExecutionRequest } from "../src/runtime/tool-runtime.js";

describe("Pi Runtime tool adapter", () => {
  it("exposes all Runtime capabilities in the required stable sequential order", () => {
    const tools = createPiRuntimeTools({ execute: async () => successResult("unused") }, testContext());

    expect(tools.map((tool) => tool.name)).toEqual([
      ToolName.LIST_FILES,
      ToolName.SEARCH_CODE,
      ToolName.READ_FILE,
      ToolName.WRITE_ARTIFACT,
    ]);
    expect(tools.every((tool) => tool.executionMode === "sequential")).toBe(true);
    expect(tools.map((tool) => (tool.parameters as { additionalProperties?: boolean }).additionalProperties)).toEqual([false, false, false, false]);
    expect(Check(tools[1].parameters, { query: "login" })).toBe(true);
    expect(Check(tools[1].parameters, { query: "login", undeclared: true })).toBe(false);
    expect(Check(tools[2].parameters, { path: "" })).toBe(false);
    expect(Check(tools[3].parameters, { path: "report.md", content: "" })).toBe(false);
  });

  it("passes Pi call identity, untrusted arguments, context, and cancellation signal to the only Runtime entry", async () => {
    const requests: ToolExecutionRequest[] = [];
    const runtime = {
      async execute(request: ToolExecutionRequest) {
        requests.push(request);
        return {
          toolCallId: request.toolCallId,
          success: true,
          code: ToolResultCode.OK,
          content: "src/auth.ts",
          artifactPath: "reports/login-flow.md",
        };
      },
    };
    const context = testContext();
    const controller = new AbortController();
    const tool = createPiRuntimeTools(runtime, context)[0];

    const result = await tool.execute("pi-call-42", { startPath: "src" }, controller.signal);

    expect(requests).toEqual([
      {
        toolCallId: "pi-call-42",
        toolName: ToolName.LIST_FILES,
        modelArguments: { startPath: "src" },
        context,
        signal: controller.signal,
      },
    ]);
    expect(result).toEqual({
      content: [{ type: "text", text: "src/auth.ts" }],
      details: {
        code: ToolResultCode.OK,
        artifactPath: "reports/login-flow.md",
      },
    });
  });

  it("throws a Pi-recognized error with Runtime's governed failure code and content", async () => {
    const tool = createPiRuntimeTools(
      {
        async execute(request: ToolExecutionRequest) {
          return {
            toolCallId: request.toolCallId,
            success: false,
            code: ToolResultCode.PATH_REJECTED,
            content: "The requested path is outside the permitted root.",
          };
        },
      },
      testContext(),
    )[2];

    await expect(tool.execute("pi-call-failure", { path: "../outside.ts" })).rejects.toMatchObject({
      name: "RuntimeToolFailure",
      code: ToolResultCode.PATH_REJECTED,
      message: "[path_rejected] The requested path is outside the permitted root.",
    } satisfies Partial<RuntimeToolFailure>);
  });
});

function testContext() {
  return createRunExecutionContext({
    projectRoot: "/project",
    repositoryRoot: "/project/repository",
    artifactsRoot: "/project/artifacts",
  });
}

function successResult(toolCallId: string) {
  return {
    toolCallId,
    success: true as const,
    code: ToolResultCode.OK,
    content: "unused",
  };
}
