import { describe, expect, it } from "vitest";
import { CliOptionsError, parseCodebaseAgentCliOptions } from "../src/agent/cli-options.js";

describe("codebase agent CLI options", () => {
  it("separates trusted flags from the positional user task", () => {
    expect(parseCodebaseAgentCliOptions(["--workspace", "/tmp/lab", "--run-id", "run-2", "--pause-after", "6", "--auto-approve", "fix", "session"])).toEqual({ workspace: "/tmp/lab", runId: "run-2", pauseAfter: 6, autoApprove: true, taskArguments: ["fix", "session"] });
  });
  it("accepts additions-only revise requests and normalizes omitted replacements", () => {
    const parsed = parseCodebaseAgentCliOptions(["--repository", "./demo-app", "--state-dir", "/tmp/state", "fix"]);
    expect(parsed).toMatchObject({ repository: "./demo-app", stateDir: "/tmp/state", taskArguments: ["fix"] });
  });
  it("parses --resume as a control-plane flag and excludes it from task input", () => {
    expect(parseCodebaseAgentCliOptions(["--state-dir", "/tmp/state", "--resume", "run-paused"])).toMatchObject({ stateDir: "/tmp/state", resumeRunId: "run-paused", taskArguments: [] });
    expect(() => parseCodebaseAgentCliOptions(["--resume", "run-paused", "new task"])).toThrow("cannot be combined with a new positional task");
    expect(() => parseCodebaseAgentCliOptions(["--run-id", "new", "--resume", "old"])).toThrow("cannot be combined");
  });
  it("parses exact pending-approval control commands", () => {
    expect(parseCodebaseAgentCliOptions(["--state-dir", "/tmp/state", "--approve", "run-waiting"])).toMatchObject({ approveRunId: "run-waiting", taskArguments: [] });
    expect(parseCodebaseAgentCliOptions(["--reject", "run-waiting"])).toMatchObject({ rejectRunId: "run-waiting", taskArguments: [] });
    expect(() => parseCodebaseAgentCliOptions(["--resume", "a", "--approve", "a"])).toThrow("mutually exclusive");
  });

  it("rejects unknown flags, missing values and invalid pause budgets", () => {
    expect(() => parseCodebaseAgentCliOptions(["--unknown"])).toThrow(CliOptionsError);
    expect(() => parseCodebaseAgentCliOptions(["--workspace"])).toThrow("--workspace requires a value.");
    expect(() => parseCodebaseAgentCliOptions(["--pause-after", "0"])).toThrow("positive integer");
  });
});
