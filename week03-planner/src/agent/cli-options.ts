/**
 * CLI 解析只把 argv 变成受信启动配置；它不访问文件系统，也不把 flag 混入用户任务。
 * workspace / runId / pauseAfter 是服务端启动边界，模型只能看到 positionals 形成的 task。
 */
export interface CodebaseAgentCliOptions {
  /** Agent 可读写、可运行测试的目标仓库；与运行状态目录完全解耦。 */
  readonly repository?: string;
  /** checkpoint、artifact、run 元数据的持久化目录。 */
  readonly stateDir?: string;
  /** 旧参数兼容别名；调用方应将它解释为 stateDir，而不是 repository 父目录。 */
  readonly workspace?: string;
  readonly runId?: string;
  /** 从本项目 checksummed checkpoint 恢复；不是 Pi 官方 CLI session 参数。 */
  readonly resumeRunId?: string;
  /** 对 waiting_for_approval checkpoint 执行冻结的精确源码写，再继续同一 Run。 */
  readonly approveRunId?: string;
  /** 拒绝冻结源码写；不执行 handler，保存拒绝事实并暂停 Run，后续必须显式 --resume。 */
  readonly rejectRunId?: string;
  readonly pauseAfter?: number;
  readonly autoApprove: boolean;
  readonly taskArguments: readonly string[];
}

export class CliOptionsError extends Error {
  constructor(message: string) { super(message); this.name = "CliOptionsError"; }
}

export function parseCodebaseAgentCliOptions(argv: readonly string[]): CodebaseAgentCliOptions {
  let repository: string | undefined;
  let stateDir: string | undefined;
  let workspace: string | undefined;
  let runId: string | undefined;
  let resumeRunId: string | undefined;
  let approveRunId: string | undefined;
  let rejectRunId: string | undefined;
  let pauseAfter: number | undefined;
  let autoApprove = false;
  const taskArguments: string[] = [];

  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--auto-approve") { autoApprove = true; continue; }
    if (value === "--repository" || value === "--state-dir" || value === "--workspace" || value === "--run-id" || value === "--resume" || value === "--approve" || value === "--reject" || value === "--pause-after") {
      const argument = argv[++index];
      if (!argument || argument.startsWith("--")) throw new CliOptionsError(`${value} requires a value.`);
      if (value === "--repository") repository = argument;
      if (value === "--state-dir") stateDir = argument;
      if (value === "--workspace") workspace = argument;
      if (value === "--run-id") runId = argument;
      if (value === "--resume") resumeRunId = argument;
      if (value === "--approve") approveRunId = argument;
      if (value === "--reject") rejectRunId = argument;
      if (value === "--pause-after") {
        const parsed = Number(argument);
        if (!Number.isInteger(parsed) || parsed < 1) throw new CliOptionsError("--pause-after must be a positive integer.");
        pauseAfter = parsed;
      }
      continue;
    }
    if (value.startsWith("--")) throw new CliOptionsError(`Unknown option: ${value}`);
    taskArguments.push(value);
  }
  if (stateDir && workspace && stateDir !== workspace) throw new CliOptionsError("--state-dir and deprecated --workspace cannot specify different paths.");
  const controlRunId = resumeRunId ?? approveRunId ?? rejectRunId;
  if ([resumeRunId, approveRunId, rejectRunId].filter(Boolean).length > 1) throw new CliOptionsError("--resume, --approve and --reject are mutually exclusive.");
  if (runId && controlRunId) throw new CliOptionsError("--run-id cannot be combined with --resume/--approve/--reject; these commands keep the checkpoint run ID.");
  if (controlRunId && taskArguments.length > 0) throw new CliOptionsError("--resume/--approve/--reject cannot be combined with a new positional task.");
  return Object.freeze({
    ...(repository === undefined ? {} : { repository }),
    ...(stateDir === undefined ? {} : { stateDir }),
    ...(workspace === undefined ? {} : { workspace }),
    ...(runId === undefined ? {} : { runId }),
    ...(resumeRunId === undefined ? {} : { resumeRunId }),
    ...(approveRunId === undefined ? {} : { approveRunId }),
    ...(rejectRunId === undefined ? {} : { rejectRunId }),
    ...(pauseAfter === undefined ? {} : { pauseAfter }),
    autoApprove,
    taskArguments: Object.freeze(taskArguments),
  });
}
