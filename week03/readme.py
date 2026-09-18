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