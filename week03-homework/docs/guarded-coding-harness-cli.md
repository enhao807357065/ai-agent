# Guarded Coding Harness CLI

正式入口是 `smolagent guarded`。它包装 smolagents `ToolCallingAgent`，并把仓库工具调用送入 `Gate → Sandbox → State / Checkpoint / Audit`。

## 任务与工具暴露

CLI 按任务类型暴露不同的工具集合：

```text
understand：list_files、search_text、read_file、write_file
fix：       list_files、search_text、read_file、write_file、run_test
```

`understand` 允许把分析写成报告，但**默认所有 write 都要人审**。它不暴露 `run_test`，因此模型用无 Shell 的目录发现、文本搜索和文件读取理解代码，不会先用 `python -c` 或 `uv run` 来列目录。

## 环境

```bash
cd ~/work/py/ai-agent/week03-homework
conda activate ai-agent

export GATEWAY_API_KEY='[REDACTED]'
export GATEWAY_BASE_URL='http://127.0.0.1:8000/v1'
export GATEWAY_MODEL_ID='chat-default'
```

查看命令：

```bash
smolagent guarded --help
```

可用子命令：

```text
understand  启动代码理解任务
fix         启动测试修复任务
approve     批准一个冻结动作
resume      消费一个已批准的冻结动作
show        显示持久化 Run 状态
audit       显示 append-only 决策链
```

## 代码理解

`understand` 默认允许生成报告文件，但写文件会冻结等待人审：
```bash
smolagent guarded understand \
  --workspace ./sample-repo \
  --database ./sample-repo/.guarded-harness.sqlite \
  --model-id chat-default \
  '阅读 src/auth.py，说明登录校验的入口、关键分支和潜在风险。'
```

该任务策略：

```text
允许：read、write（write 默认人审）
不暴露：run_test / execute
```

模型请求写入时，Gate 在 handler 前返回并冻结：

```json
{
  "ok": false,
  "tool_call_id": "原 provider tool_call_id",
  "code": "APPROVAL_REQUIRED",
  "approval_id": "<APPROVAL_ID>"
}
```

CLI 输出 `status: waiting_approval`、`run_id` 和完整 approval payload。随后运行下文的 `approve` 与 `resume`。

## 测试修复

默认要求人工审批：

```bash
smolagent guarded fix \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite \
  --model-id chat-default \
  '运行 tests/test_mathlib.py，定位失败原因，修复 src/mathlib.py，然后复跑测试。'
```

本地学习时可跳过审批：

```bash
smolagent guarded fix \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite \
  --no-approval \
  '运行测试，修复失败，再复跑测试。'
```

期望模型流程：

```text
run_test（红）
→ read_file（读测试和源码）
→ write_file（修复）
→ run_test（绿）
→ final_answer
```

服务端自动产生：

```text
read 成功                 → NOTE Evidence
write 成功                → CHANGE Evidence
execute 且 exit_code == 0 → TEST Evidence
```

`testing_fix` 最终验收要求同时存在 `CHANGE` 和 `TEST` Evidence；缺失时结果为 `FINAL_ACCEPTANCE_FAILED`。

## 审批与恢复

启动命令会输出 JSON，其中包含 `run_id` 与 SQLite `database`。

查看等待审批的 Run：

```bash
smolagent guarded show \
  --run-id <RUN_ID> \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite
```

批准冻结动作：

```bash
smolagent guarded approve \
  --run-id <RUN_ID> \
  --approval-id <APPROVAL_ID> \
  --actor reviewer \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite
```

消费同一份冻结参数：

```bash
smolagent guarded resume \
  --run-id <RUN_ID> \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite
```

查看审计链：

```bash
smolagent guarded audit \
  --run-id <RUN_ID> \
  --workspace /tmp/guarded-demo \
  --database /tmp/guarded-demo/.guarded-harness.sqlite
```

`resume` 使用原始 `tool_call_id` 和原冻结参数。已完成的调用被 Gate 拒绝为 `REPEATED_TOOL_CALL`，不会重放。

## 当前边界

当前 CLI 能执行和恢复一个已批准的冻结动作；它还没有恢复 smolagents 的完整 Agent Memory / 模型响应 cursor 后自动进入下一轮 Agent Loop。因此 `resume` 不等于完整 Agent Run 的无缝续跑。

## 验证

```bash
pytest -q
```

当前测试覆盖：Gate、Sandbox、State/Checkpoint、Audit、ToolCallingAgent 接缝、Completion 和正式 CLI。
