# Guarded Coding Harness 运行说明与验收记录

本说明面向 `week03-homework` 中新增的 Guarded Coding Harness。它不重写 smolagents 核心循环，而是在 `ToolCallingAgent` 的仓库工具调用边界增加：

```text
Model Tool Call
  → Gate
  → Sandbox
  → StateStore / CheckpointStore / AuditLog
  → Tool Result / Evidence
  → Final Acceptance
```

## 1. 源码与测试

### 1.1 主要源码

```text
src/smolagents/guarded_harness/
├── audit.py          # SQLite append-only 审计事件
├── checkpoint.py     # State 快照与 checksum
├── cli.py            # smolagent guarded 正式 CLI
├── gate.py           # 参数、预算、重复调用、审批、final answer 门禁
├── harness.py        # smolagents Adapter、运行恢复、最终验收、trace
├── sandbox.py        # 路径、软链接、敏感目录、命令 allowlist 与进程组终止
└── state_store.py    # SQLite Run、Evidence、Approval、预算、完成调用
```

CLI 顶层接线：

```text
src/smolagents/cli.py
```

正式命令：

```bash
smolagent guarded --help
```

可用子命令：

```text
understand  代码理解，可输出报告
fix         测试修复，可运行受限测试命令
approve     按 approval_id 批准冻结动作
resume      消费已批准的冻结动作
show        查看持久化 Run State
audit       查看 append-only 审计事件
trace       查看 State、Gate、Sandbox、Approval、Result、Evidence 聚合视图
```

### 1.2 Guarded Harness 测试

```text
tests/test_gate.py              # 参数、重复、风险与审批门禁
tests/test_sandbox.py           # 路径、软链接、命令 allowlist、超时进程组
tests/test_state_checkpoint.py  # checkpoint、UNKNOWN_RESULT、workspace freshness
tests/test_audit.py             # 拒绝审计字段与 trace 聚合
tests/test_completion.py        # 交付物、测试 freshness、预算停止
tests/test_harness_loop.py      # ToolCallingAgent 接缝、final answer、max steps
tests/test_guarded_cli.py       # CLI、approval_id 权威与 --no-approval
```

## 2. 环境与运行前提

```bash
cd ~/work/py/ai-agent/week03-homework
conda activate ai-agent
```

### 2.1 经由现有 Gateway

当本机已有 OpenAI-compatible Gateway 服务时：

```bash
export GATEWAY_API_KEY='[REDACTED]'
export GATEWAY_BASE_URL='http://127.0.0.1:8000/v1'
export GATEWAY_MODEL_ID='chat-default'
```

运行 CLI 时可以使用环境变量默认值，也可以显式传入：

```bash
smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id "$GATEWAY_MODEL_ID" \
  --api-base "$GATEWAY_BASE_URL" \
  --no-approval \
  '阅读代码并写入 artifacts/report.md。'
```

### 2.2 直连 DeepSeek 官方 OpenAI-compatible API

不需要专门启动 Gateway。CLI 继续读取 `GATEWAY_API_KEY`，只需将该变量设为 DeepSeek 官方 API Key，并将同一组 `GATEWAY_*` 环境变量指向 DeepSeek 官方 OpenAI-compatible endpoint：

```bash
export GATEWAY_API_KEY='[REDACTED]'
export GATEWAY_BASE_URL='https://api.deepseek.com/v1'
export GATEWAY_MODEL_ID='deepseek-v4-pro'
```

然后使用**同一条** Guarded Harness 命令：

```bash
smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id "$GATEWAY_MODEL_ID" \
  --api-base "$GATEWAY_BASE_URL" \
  --no-approval \
  '阅读 src/auth.py，说明登录校验入口和风险，并将报告写到 artifacts/login-analysis.md。'
```

也可以不用导出环境变量，直接对单次运行指定官方 endpoint；但 API Key 仍只应从环境变量读取：

```bash
export GATEWAY_API_KEY='[REDACTED]'

smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id deepseek-v4-pro \
  --api-base https://api.deepseek.com/v1 \
  --no-approval \
  '阅读 src/auth.py，并将分析写到 artifacts/login-analysis.md。'
```

### 2.3 DeepSeek 工具调用兼容性

smolagents 的 `OpenAIModel` 默认对有工具的请求发送：

```text
tool_choice = required
```

DeepSeek 官方 API 在 **thinking mode** 下拒绝 `required` 或指定工具名，因此会返回：

```text
Thinking mode does not support this tool_choice
```

Guarded Harness 对 `model_id` 中包含 `deepseek` 的模型自动做两项兼容处理：

```text
tool_choice = required
thinking = disabled（默认）
```

因此推荐使用当前验证可运行的模型：

```bash
export GATEWAY_MODEL_ID='deepseek-v4-pro'
```

默认关闭 thinking 的原因是当前 Harness 进行多轮工具调用时，尚未持久化和回传 DeepSeek 的 `reasoning_content`。如需实验 thinking mode，可显式打开：

```bash
smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id deepseek-v4-pro \
  --api-base https://api.deepseek.com/v1 \
  --deepseek-thinking enabled \
  --no-approval \
  '阅读 src/auth.py，并将分析写到 artifacts/login-analysis.md。'
```

这是实验路径，不应作为当前多轮受治理任务的默认方式。
DeepSeek 的官方 standalone probe 也在仓库中：

```bash
export DEEPSEEK_API_KEY='[REDACTED]'
export DEEPSEEK_BASE_URL='https://api.deepseek.com/v1'
export DEEPSEEK_MODEL_ID='deepseek-v4-pro'

python examples/deepseek_official_probe.py
```

期望输出：

```text
deepseek-official-api-ok
```

不要把真实 API Key 写入源码、SQLite、Git、trace、shell 历史或运行记录。示例中的 `[REDACTED]` 是占位符。

## 3. 两类仓库任务

### 3.1 代码理解并输出报告

默认情况下，`write_file` 需要人工审批：

```bash
smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id "$GATEWAY_MODEL_ID" \
  '阅读项目代码，说明登录校验的入口、关键分支和风险；将报告写到 artifacts/login-analysis.md。'
```

本地学习、无副作用演示可显式关闭审批：

```bash
smolagent guarded understand \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite \
  --model-id "$GATEWAY_MODEL_ID" \
  --no-approval \
  '阅读项目代码，说明登录校验的入口、关键分支和风险；将报告写到 artifacts/login-analysis.md。'
```

`understand` 可用工具：

```text
list_files, search_text, read_file, write_file
```

它不暴露 `run_test`，因此不会以 Shell 命令替代代码阅读。

### 3.2 测试修复

```bash
smolagent guarded fix \
  --workspace ./samples/2_bug_fix \
  --database ./samples/2_bug_fix/.guarded-harness.sqlite \
  --model-id "$GATEWAY_MODEL_ID" \
  --no-approval \
  '先读取 demo-app/package.json；运行测试时使用 cwd=demo-app。定位会话过期边界问题，修复源码，再复跑测试，并写入 artifacts/login-fix.md。'
```

`fix` 的 `run_test` 参数包含 `argv` 和 workspace-relative `cwd`。对于该嵌套 Node 项目，应使用：

```json
{"argv": ["npm", "test"], "cwd": "demo-app"}
```

预期受治理流程：

```text
run_test（红）
→ read_file（源码与测试）
→ write_file（修复）
→ run_test（绿）
→ final_answer
```

最终验收要求：

```text
code_understanding：若 prompt 明确要求 artifacts/docs/reports 交付物，
                    文件必须存在且必须有 server-owned CHANGE Evidence。

testing_fix：必须有 CHANGE Evidence；必须有与当前 workspace digest 一致的
             成功 TEST Evidence。源码在测试后再次变化时，旧 TEST Evidence 失效。
```

## 4. 审批、恢复和观测

等待审批后，命令输出中会给出 `approval_id`、`run_id` 和数据库路径。

```bash
# 1. 批准。approval_id 是操作授权的主键；run_id/workspace 由服务端反查。
smolagent guarded approve \
  --approval-id <APPROVAL_ID> \
  --actor reviewer \
  --database ./samples/1_login_demo/.guarded-harness.sqlite

# 2. 消费同一条已经批准的冻结调用。
smolagent guarded resume \
  --approval-id <APPROVAL_ID> \
  --database ./samples/1_login_demo/.guarded-harness.sqlite

# 3. 查看单个 Run 的完整关联视图。
smolagent guarded trace \
  --run-id <RUN_ID> \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite
```

恢复保护：

```text
已完成调用：Gate 以 provider tool_call_id 阻止重复执行。
执行开始、尚未保存结果：恢复为 UNKNOWN_RESULT，停止等待人工核对。
工作区摘要变化：恢复为 WORKSPACE_CHANGED，旧审批不得继续消费。
```

## 5. pytest -q 实际结果

本说明写入前，在项目根目录实际执行：

```bash
/Users/lianghao/anaconda3/envs/ai-agent/bin/python -m pytest -q
```

实际结果：

```text
32 passed in 3.13s
```

该次执行包含 Guarded Harness 的确定性 P0/P1 测试，以及真实模型运行所用的最小样例测试：

```text
tests/test_audit.py
...
tests/test_completion.py
.....
tests/test_gate.py
.....
tests/test_guarded_cli.py
....
tests/test_harness_loop.py
...
tests/test_sandbox.py
......
tests/test_state_checkpoint.py
.....
tmp/real-model-acceptance/test_auth.py
.
```

说明：`tmp/real-model-acceptance` 是真实模型验收样例。若不希望临时样例被默认 `pytest` 收集，可删除该目录或在 pytest 配置中排除 `tmp/`。

## 6. 完整真实模型运行记录

### 6.1 输入与命令

实际模型 Run：

```text
run_id: 81e1677b015b47738dddb94f74d0cd2d
task_kind: code_understanding
workspace: tmp/real-model-acceptance
model_id: chat-default
```

命令：

```bash
smolagent guarded understand \
  --workspace tmp/real-model-acceptance \
  --database tmp/real-model-acceptance/.guarded-harness.sqlite \
  --model-id chat-default \
  --api-base http://127.0.0.1:8000/v1 \
  --max-steps 12 \
  --no-approval \
  '阅读 src/auth.py 与 test_auth.py，说明登录校验的入口、关键分支和一个潜在风险。必须将中文报告写入 artifacts/login-analysis.md。完成前确认该文件已写入。'
```

输入仓库：

```text
tmp/real-model-acceptance/
├── src/auth.py
└── test_auth.py
```

`src/auth.py`：

```python
def validate_login(username: str, password: str) -> bool:
    return bool(username and password)
```

### 6.2 实际结果

```text
status:      completed
stop_reason: FINAL_ACCEPTANCE_PASSED
tool_calls:  5 / 50
artifact:    tmp/real-model-acceptance/artifacts/login-analysis.md
size:        2066 bytes
```

模型实际产生的 Evidence：

```text
NOTE    call_jtwgnfvm9meigwhcqwu2kial  list_files
NOTE    call_3yg7xlukn18mk0t04nu7er9a  read_file(test_auth.py)
NOTE    call_g7yqfq9ypbhj641vywwn7dmx  read_file(src/auth.py)
CHANGE  call_b4tyepfnvtanh5gujqu0sk4p  write_file(artifacts/login-analysis.md)
NOTE    call_cx9n2onjymppk1g6p1f2u2e1  read_file(artifacts/login-analysis.md)
```

模型报告的核心结论与源码相符：

```text
入口：src/auth.py::validate_login
关键分支：bool(username and password)
风险：任意非空用户名与密码组合都会返回 True；未做真实凭据校验。
```

交付物在模型写入后被再次读取，因此可以交叉核对：

```text
CHANGE Evidence
↔ write_file tool result: wrote artifacts/login-analysis.md
↔ 实际文件存在
↔ 后续 read_file tool result
↔ final acceptance: FINAL_ACCEPTANCE_PASSED
```

## 7. 审计记录示例

以下是上面真实 Run 中 `write_file` 的完整 Gate 决策链。审计记录的 `toolCallId` 可将请求、执行、结果和 Evidence 关联起来。

```json
[
  {
    "sequence": 18,
    "event": "received",
    "actor": "system",
    "toolCallId": "call_b4tyepfnvtanh5gujqu0sk4p",
    "tool": "write_file"
  },
  {
    "sequence": 19,
    "event": "allowed",
    "actor": "system",
    "toolCallId": "call_b4tyepfnvtanh5gujqu0sk4p",
    "risk": "write"
  },
  {
    "sequence": 20,
    "event": "executing",
    "actor": "system",
    "toolCallId": "call_b4tyepfnvtanh5gujqu0sk4p"
  },
  {
    "sequence": 21,
    "event": "result",
    "actor": "system",
    "toolCallId": "call_b4tyepfnvtanh5gujqu0sk4p",
    "ok": true
  }
]
```

同一个 Run 的 final answer 也经过 Gate：

```json
{
  "sequence": 28,
  "event": "result",
  "decision": "accepted",
  "toolCallId": "call_v4lkx65mgw36kdwre5wrk2bh",
  "ok": true
}
```

被拒绝调用会追加显式字段：

```json
{
  "event": "rejected",
  "rule": "INVALID_ARGUMENT",
  "decision": "rejected",
  "actor": "system",
  "toolCallId": "<provider-tool-call-id>"
}
```

读取真实 Run 的完整记录：

```bash
smolagent guarded trace \
  --run-id 81e1677b015b47738dddb94f74d0cd2d \
  --workspace ./tmp/real-model-acceptance \
  --database ./tmp/real-model-acceptance/.guarded-harness.sqlite
```

## 8. Checkpoint 恢复记录

实际执行的确定性恢复用例：

```bash
pytest -q tests/test_state_checkpoint.py::test_checkpoint_resume_does_not_replay_completed_write_and_restores_business_state
```

该测试模拟以下 Run：

```text
1. 创建 testing_fix Run。
2. 调用 write_marker(value="v1")，handler 真实调用一次。
3. 保存 checkpoint，reason = safe_tool_boundary。
4. 使用新的 GuardedCodingHarness 进程对象从同一 SQLite database 恢复。
```

恢复后的真实断言：

```text
steps:
[{"name": "fix", "status": "completed"}]

budget.tool_calls:
1

completed_tool_calls["call_once"].ok:
true

handler calls:
["v1"]

resumed.resume():
None
```

结论：恢复找回了 Step、预算和已完成调用；已完成的写操作没有重放。

另外两个恢复保护由同一测试文件覆盖：

```text
test_recovery_marks_started_write_without_result_unknown_and_never_replays
  - in_flight write 未保存结果
  - 恢复后：waiting_verification / UNKNOWN_RESULT
  - 不自动重放。

test_recovery_stops_when_workspace_changed_after_checkpoint
  - checkpoint 后工作区被外部改动
  - 恢复后：waiting_verification / WORKSPACE_CHANGED
  - 旧审批不可批准或消费。
```

运行全部 checkpoint 验收：

```bash
pytest -q tests/test_state_checkpoint.py
```

### 8.1 持久化 Checkpoint 实例：understand Run

真实 `code_understanding` Run：

```text
run_id:     9a2501bb18134b9b86fe6815f5ef6ebc
workspace:  samples/1_login_demo
database:   samples/1_login_demo/.guarded-harness.sqlite
checkpoint: final_acceptance
checksum:   17836850b89722949c719e4395f5efa3117d8efba90319bd62d0786d191b442f
```

该 Checkpoint 的关键持久化状态：

```text
status: completed
stop_reason: FINAL_ACCEPTANCE_PASSED
budget.tool_calls: 7 / 50
in_flight_tool_call: null
pending_approval: null
completed_call_sequence: 7 个 provider tool_call_id
Evidence: 5 个 NOTE + 1 个 CHANGE
workspace_digest: 7687a08bb2a7a000e46275ef0709d2f712ce0f3ebfd2e5b3f053b93f08654cf9
```

因此恢复程序可先校验 Checkpoint checksum；再恢复完成调用、预算、Evidence 和 workspace digest，避免重放已完成的工具调用。

## 9. 近期真实模型运行补充记录

本节记录两条真实 Guarded Harness Run。凭据未写入命令、审计、Checkpoint 或本文档。

### 9.1 Understand 完整运行：任务前提错误时依据仓库事实完成分析

```text
run_id:     9a2501bb18134b9b86fe6815f5ef6ebc
kind:       code_understanding
workspace:  samples/1_login_demo
database:   samples/1_login_demo/.guarded-harness.sqlite
prompt:     阅读 src/auth.py，并将分析写到 artifacts/login-analysis.md。
status:     completed
stop_reason: FINAL_ACCEPTANCE_PASSED
tool_calls: 7 / 50
```

完整关键链路：

```text
read_file(src/auth.py)
  → FileNotFoundError；该仓库实际为 TypeScript/Express，不存在该 Python 文件
list_files(.)
  → 发现 login route、auth service、auth middleware、token 等实际认证模块
read_file(认证中间件 / repository / database / profile route)
write_file(artifacts/login-analysis.md)
final_answer
  → server-owned final acceptance: FINAL_ACCEPTANCE_PASSED
```

交付物与证据：

```text
artifact: artifacts/login-analysis.md
CHANGE tool_call_id: call_00_T61zX5Two5fPaNMK2DEp4552
write result: wrote artifacts/login-analysis.md
workspace digest after write:
7687a08bb2a7a000e46275ef0709d2f712ce0f3ebfd2e5b3f053b93f08654cf9
```

该 Run 没有假装读取到不存在的 `src/auth.py`。它通过目录发现纠正任务前提，转而分析仓库实际的 Express 登录、Token 与认证中间件实现；最终以真实交付物、CHANGE Evidence 和 final acceptance 完成。

### 9.2 Fix 完整运行：红绿测试完成，但报告写入使旧 TEST Evidence 失效

```text
run_id:     ce5836cd9427464f8c1548243dc1572d
kind:       testing_fix
workspace:  samples/2_bug_fix
database:   samples/2_bug_fix/.guarded-harness.sqlite
status:     failed
stop_reason: FINAL_ACCEPTANCE_FAILED
tool_calls: 12 / 50
```

真实执行链路：

```text
read package.json、login/session 源码、边界测试
run_test
  → 红色 baseline；failure_kind=TEST_FAILED
write_file(demo-app/src/auth/session-policy.ts)
  → CHANGE，修复为毫秒时间戳精确比较
run_test
  → exit_code=0；8 pass / 0 fail；TEST Evidence
write_file(artifacts/login-fix.md)
  → 新的 CHANGE
final_answer
  → FINAL_ACCEPTANCE_FAILED
```

关键 Evidence：

```text
CHANGE source:
call_00_BS8fpp9B6lu8P8hUkKL28524
workspace_digest: 7a8136c14cc2249dc291ec4d3ff59923c769d6ba96a8fbc77fed81437178dbf7

TEST green:
call_00_j07euK0XVfIgBvjIXPz65012
exit_code: 0
pass: 8
fail: 0
workspace_digest: 7a8136c14cc2249dc291ec4d3ff59923c769d6ba96a8fbc77fed81437178dbf7

CHANGE report:
call_00_2xcQT4hjIbkXkVSvxHvO3026
result: wrote artifacts/login-fix.md
workspace_digest: 4b37aa1b06a51d930ab1f359b32df5b0400674c371dfb86bb2b9aeb69dc38e6a
```

最终验收拒绝原因：报告写入改变 workspace digest，而成功 TEST Evidence 对应报告写入前的 digest。因此 `8 pass / 0 fail` 不足以代表整体验收成功：

```text
issue: successful TEST evidence is required for the current workspace version
```

该 Run 是真实的验收失败样本。当前语义下，如果报告也写入 watched workspace，报告写入后必须再运行测试；后续可评估是否将纯文档产物排除出代码测试 freshness 范围。

### 9.3 真实审计记录：成功写入与被拒绝 final answer

Fix Run 中的关键 audit 链如下；`toolCallId` 可关联 Gate、State 和 Evidence：

```json
[
  {
    "event": "received",
    "toolCallId": "call_00_BS8fpp9B6lu8P8hUkKL28524",
    "tool": "write_file"
  },
  {
    "event": "allowed",
    "toolCallId": "call_00_BS8fpp9B6lu8P8hUkKL28524",
    "risk": "write"
  },
  {
    "event": "executing",
    "toolCallId": "call_00_BS8fpp9B6lu8P8hUkKL28524"
  },
  {
    "event": "result",
    "toolCallId": "call_00_BS8fpp9B6lu8P8hUkKL28524",
    "ok": true
  },
  {
    "event": "received",
    "toolCallId": "call_00_HDfMNHCJ1vEAtb0AukK85336",
    "tool": "final_answer"
  },
  {
    "event": "rejected",
    "toolCallId": "call_00_HDfMNHCJ1vEAtb0AukK85336",
    "rule": "FINAL_ACCEPTANCE_FAILED",
    "decision": "rejected",
    "actor": "system",
    "issues": ["successful TEST evidence is required for the current workspace version"]
  }
]
```

这证明 final answer 是不可信完成声明；服务端基于当前 digest、CHANGE 和 TEST Evidence 独立判定验收。

读取两条真实 Run 的聚合视图：

```bash
smolagent guarded trace \
  --run-id 9a2501bb18134b9b86fe6815f5ef6ebc \
  --workspace ./samples/1_login_demo \
  --database ./samples/1_login_demo/.guarded-harness.sqlite

smolagent guarded trace \
  --run-id ce5836cd9427464f8c1548243dc1572d \
  --workspace ./samples/2_bug_fix \
  --database ./samples/2_bug_fix/.guarded-harness.sqlite
```

## 10. 当前边界

已实现：

```text
工具级 checkpoint、审批恢复、执行意图、UNKNOWN_RESULT、
workspace freshness、重复写保护、最终交付物验收、审计和 trace。
```

尚未实现：

```text
完整持久化 smolagents Agent Memory / provider response cursor，
以及恢复后自动继续后续模型轮次。
```

当前 `resume` 的边界是：只消费一次已经批准且冻结的动作；它不是完整 Agent Loop 的无缝自动续跑。
