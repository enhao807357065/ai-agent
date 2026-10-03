# ┌───────────── CLI / Web / Telegram UI ──────────────┐
# │ 用户：“解释 auth 模块，找出登录失败的原因”             │
# └────────────────────┬───────────────────────────────┘
#                      │ user message
# ┌────────────────────▼───────────────────────────────┐
# │                 CodebaseAgent                       │
# │                                                     │
# │  AgentContext                                       │
# │  - messages: 可回放的 transcript                    │
# │  - tools: 本次 run 可执行工具的快照                   │
# │                                                     │
# │  AgentLoopConfig                                    │
# │  - model                                            │
# │  - convertToLlm                                     │
# │  - transformContext       ← 压缩 / 注入 repo 摘要    │
# │  - beforeToolCall        ← 权限、路径、审批          │
# │  - afterToolCall         ← 脱敏、截断、审计          │
# └───────────────┬─────────────────────────────────────┘
#                 │
#                 ▼
#         Pi `runAgentLoop`
#                 │
#       ┌─────────┴───────────┐
#       ▼                     ▼
#   LLM streaming        Tool Runtime
#                               │
#       ┌───────────────────────┼────────────────────────┐
#       ▼                       ▼                        ▼
#   search_code            read_file                run_command
#   (ripgrep)              (root sandbox)          (allowlist)
#       │                       │                        │
#       └───────────────────────▼────────────────────────┘
#                          Tool Result
#                               │
#                               ▼
#                      回填给 LLM 的 transcript


# week03/
# ├── node_modules/             # 已存在
# ├── package.json              # 已存在，保留不动
# ├── package-lock.json         # 已存在
# ├── read.py                   # 已存在，保留不动
# ├── src/
# │   ├── agent/                # AgentContext、Loop 配置、策略（后续）
# │   ├── model/                # TAL Provider / 模型注册（后续）
# │   ├── runtime/              # sandbox、验证、trace（后续）
# │   └── tools/                # 本地工具定义（后续）
# └── tests/                    # 测试（后续）


# 一个 Run
#   包含多个 Turn
#
# 一个 Turn
#   可以包含 0～N 个 Tool Call
#
# 一个 Step
#   可以跨越 1～N 个 Turn
#   也可以在一个 Turn 内完成


# 实际的planner的时序
# ① CLI 读取任务文本
#    └─ task.value
#
# ② CLI 创建 Plan v1
#    └─ diagnose-task: pending
#
# ③ runCodebaseAgent 初始化模型上下文
#    ├─ 通用 System Prompt
#    ├─ 用户任务文本
#    ├─ 工具 Schema
#    └─ [PLAN_STATE] v1
#
# ④ 模型请求 start diagnose-task
#    └─ PlanStore.start()
#       └─ diagnose-task: running
#
# ⑤ 模型 read/search 代码与测试
#    └─ 得到真实事实
#
# ⑥ 模型提交 note Evidence 并 complete diagnosis
#    └─ PlanStore.complete()
#       └─ diagnose-task: completed
#
# ⑦ 模型依据 Evidence 提议 revise_plan
#    └─ additions:
#       ├─ verify-unit-tests
#       └─ verify-build
#
# ⑧ PlanStore 原子校验并发布 v2
#
# ⑨ prepareNextTurn 替换 Plan Snapshot
#    └─ [PLAN_STATE] v2
#       └─ ready=[verify-unit-tests]
#
# ⑩ 模型执行 verification Steps
#    ├─ start
#    ├─ run_tests
#    ├─ 服务端写入 test Evidence
#    └─ complete
#
# ⑪ Completion Contract 检查
#    └─ 全部 Step completed
#       └─ completion_contract_satisfied


# 先区分四个概念
#
# **Run**
# • 谁控制: Agent Runtime
# • 含义: 一次完整的 Agent 执行，例如一次 npm start -- "..."
#
# **Turn**
# • 谁控制: Pi Agent Loop
# • 含义: 一次模型请求 + 模型返回 + 本轮 Tool Calls
#
# **Tool Call**
# • 谁控制: 模型提出
# • 含义: 对 read_file、run_tests、update_plan_step 等工具的一次调用
#
# **Plan Step**
# • 谁控制: PlanStore
# • 含义: 一个可验收的工作节点，例如“诊断问题”“修改代码”“运行验证”