import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 服务端为一次 Run 构造的可信路径边界。
 *
 * 其中任何字段都不能由模型工具参数覆盖。
 */
export interface RunExecutionContext {
  readonly projectRoot: string;
  readonly repositoryRoot: string;
  readonly artifactsRoot: string;
  readonly taskArtifactPath: string;
}

export interface RunExecutionContextOverrides {
  readonly projectRoot?: string;
  readonly repositoryRoot?: string;
  readonly artifactsRoot?: string;
  readonly taskArtifactPath?: string;
}

const moduleDirectory = dirname(fileURLToPath(import.meta.url));
const defaultProjectRoot = resolve(moduleDirectory, "../..");

/**
 * 创建一次 Run 的路径边界。
 *
 * 这是纯函数：只解析字符串，不访问文件系统，也不依赖 process.cwd()。
 */
export function createRunExecutionContext(
  overrides: RunExecutionContextOverrides = {},
): RunExecutionContext {
  const projectRoot = resolve(overrides.projectRoot ?? defaultProjectRoot);

  return Object.freeze({
    projectRoot,
    repositoryRoot: resolve(
      overrides.repositoryRoot ?? resolve(projectRoot, "login_demo"),
    ),
    artifactsRoot: resolve(
      overrides.artifactsRoot ?? resolve(projectRoot, "artifacts"),
    ),
    taskArtifactPath: overrides.taskArtifactPath ?? "login-flow.md",
  });
}
