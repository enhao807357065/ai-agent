import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { createRunExecutionContext } from "../src/runtime/execution-context.js";

describe("createRunExecutionContext", () => {
  it("derives defaults from this module's location rather than process.cwd", () => {
    const context = createRunExecutionContext();
    const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

    expect(context).toEqual({
      projectRoot,
      repositoryRoot: resolve(projectRoot, "login_demo"),
      artifactsRoot: resolve(projectRoot, "artifacts"),
      taskArtifactPath: "login-flow.md",
    });
  });

  it("allows independent field overrides", () => {
    const context = createRunExecutionContext({
      projectRoot: "/isolated/project",
      repositoryRoot: "/isolated/repository",
      artifactsRoot: "/isolated/artifacts",
      taskArtifactPath: "reports/login.md",
    });

    expect(context).toEqual({
      projectRoot: "/isolated/project",
      repositoryRoot: "/isolated/repository",
      artifactsRoot: "/isolated/artifacts",
      taskArtifactPath: "reports/login.md",
    });
  });
});
