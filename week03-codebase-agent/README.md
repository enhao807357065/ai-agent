# week03/codebase-agent

一个可离线逐字重跑的 Codebase Agent：十件仓库工具都经过 TypeBox 契约、策略、预算、审计和沙箱；读操作自动执行，写入/执行操作按照任务策略进入审批。

## 安装与测试

```bash
npm ci
npm run check
npm test
```

## 离线验收

任务一先停在 `write_file` 的审批门口。审批单绑定完整工具参数、调用 ID、租户与工作区；`approvals decide` 只做决定，`approvals execute` 通过 SQLite 原子领取并至多执行一次。执行成功后可用 `agent --resume <run-id>` 恢复被冻结的对话继续后续工作；单子仍为 pending/approved 时恢复会保持拒绝，rejected/failed 或 `executing`（崩溃恢复需人工核查）会失败关闭：

```bash
node --import tsx src/cli.ts agent "解释登录调用链" --task understand --decider mock
node --import tsx src/cli.ts approvals list --task understand
node --import tsx src/cli.ts approvals decide <approval-id> --decision approve --actor reviewer --roles maintainer --task understand
node --import tsx src/cli.ts approvals execute <approval-id> --task understand
node --import tsx src/cli.ts agent --resume <run-id> --task understand --decider mock
```

完成后交付物在 `samples/1_login_demo/artifacts/login-flow.md`。

任务二按受治理的红—绿循环执行：先只在 `tests/generated/` 写入一条新的同日精确到期边界测试；再单独运行该测试，确认它因已知的按日比较缺陷失败；随后只补丁 `src/auth/session-policy.ts`；最后跑完整套件（它也覆盖现有登录集成测试）。`tests/` 中已有的测试不会被 Agent 改写，沙箱只对 `tests/generated/**` 开放测试写入。

每个 `write_file`、`apply_patch` 和 `run_test` 都会停在审批门口。该任务配置为 HTTP/LangGraph 审批：保持原 `agent` 进程运行，在另一个终端对当前工单执行 `approvals decide`；批准后，原 Agent 自动继续到下一道门。不要对一个正被 HTTP Agent 等待的工单手工 `approvals execute`，因为该运行会消费同一份绑定授权。

```bash
# 终端一：启动 LangGraph 审批服务（若尚未启动，见下方“LangGraph 审批服务”）
# 终端二：启动受治理的脚本化修复循环
HITL_MODE=http HITL_API_KEY=dev-key node --import tsx src/cli.ts agent "修复失败用例" --task fix_tests --decider mock

# 终端三：每次出现新工单都查询并批准；原 Agent 会继续到下一道审批门
HITL_MODE=http HITL_API_KEY=dev-key node --import tsx src/cli.ts approvals tickets --task fix_tests
HITL_MODE=http HITL_API_KEY=dev-key node --import tsx src/cli.ts approvals decide <approval-id> --decision approve --actor reviewer --roles tech_lead --task fix_tests
```

如需在样本仓库中单独复核红绿证据，可运行：

```bash
cd samples/2_bug_fix/demo-app
npm test -- tests/generated/session-expiry-boundary.test.ts  # 补丁前失败，补丁后通过
npm test                                                   # 全量：边界测试与既有登录集成测试
```

## 模型接入

默认使用本机 `ai-agent/app` Gateway 的 `chat-default`：

```bash
export GATEWAY_API_KEY='你的 Gateway Key'
npm run cli -- --provider gateway agent "解释登录调用链" --task understand --decider real
```

也可以明确绕过 Gateway，直连 DeepSeek 原生 Chat Completions API：

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek Key'
npm run cli -- --provider deepseek agent "解释登录调用链" --task understand --decider real
```

`--provider gateway|deepseek` 决定 base URL、模型名、凭据变量与 reasoning 兼容规则；不会因为设置了 DeepSeek Key 而把 Gateway 路径改成直连。可用环境变量覆盖：`MODEL_PROVIDER`、`GATEWAY_BASE_URL`、`GATEWAY_MODEL_ID`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL_ID`。

## 持久运行与 `--plan`

`runs list` 展示持久运行的状态投影，`runs show <run-id>` 会先校验 checkpoint checksum 再输出完整运行状态。`agent --plan` 会创建 `plan:<run-id>`，并把首次受治理工具调用关联到唯一 `execute` 步骤：读操作记为 note、成功写操作记为 change 且推进代码版本、`run_test` 记为 test 和验证结果。收口时未完成步骤、失败测试或过期验证证据都会成为验收缺项，不能仅靠模型文本宣称完成。

## LangGraph 审批服务

```bash
cd hitl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn service:app --host 127.0.0.1 --port 8383
```

把 `config/platform.yaml` 的 `hitl.mode` 设为 `http` 后，审批请求会经 `POST /tickets` 进入 LangGraph 规则图；图拒绝 self-approval，且审批者角色必须属于任务的 approvers。工单的等待状态和 CAS 决定结果由 SQLite 持久化，因此服务重启后仍可查询和答复工单。这里**没有使用 LangGraph interrupt 或 checkpointer 作为等待恢复机制**：运行时轮询 SQLite 工单；HTTP 模式下原 Agent 进程应保持运行。

`eval` 子命令刻意未提供：它属于明确不做的跨任务批量评估入口。
