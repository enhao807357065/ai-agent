import { describe, expect, it } from "vitest";
import { readTaskInput } from "../src/agent/task-input.js";

describe("official CLI task input", () => {
  const defaultTask = "default task";

  it("prefers a non-empty positional task over the environment variable", () => {
    expect(readTaskInput(["audit", "the", "repository"], "environment task", defaultTask)).toEqual({
      value: "audit the repository",
      source: "argv",
    });
  });

  it("uses CODEBASE_AGENT_TASK when no positional task is supplied", () => {
    expect(readTaskInput([], "  inspect auth middleware  ", defaultTask)).toEqual({
      value: "inspect auth middleware",
      source: "env",
    });
  });

  it("falls back to the documented default task when both sources are blank", () => {
    expect(readTaskInput(["  "], " ", defaultTask)).toEqual({
      value: defaultTask,
      source: "default",
    });
  });
});
