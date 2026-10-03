/**
 * 正式 CLI 的任务输入解析。
 * 优先级：命令行位置参数 > CODEBASE_AGENT_TASK 环境变量 > 调用方提供的默认任务。
 * 该模块不读取仓库、不启动模型，便于独立测试。
 */
export type TaskSource = "argv" | "env" | "default";

export interface TaskInput {
  readonly value: string;
  readonly source: TaskSource;
}

export function readTaskInput(
  argv: readonly string[],
  environmentTask: string | undefined,
  defaultTask: string,
): TaskInput {
  const argument = argv.join(" ").trim();
  if (argument) return { value: argument, source: "argv" };

  const fromEnvironment = environmentTask?.trim();
  if (fromEnvironment) return { value: fromEnvironment, source: "env" };

  return { value: defaultTask, source: "default" };
}
