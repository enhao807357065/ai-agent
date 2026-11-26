# 对话轨迹图 · 完整使用说明（环境配置 → 真实运行 → 分析）

项目：`~/work/py/ai-agent/week05-codebase-agent-homework`
读完这份文档，你能从零配好环境、真实跑一次对话、在图形界面里把它拆开分析。
文中的数字都来自本机实测（Node v25.2.1 / macOS），换机器会变，判断方法不变。

---

## 0. 五分钟跑通（零凭据路径）

先让一条完整对话跑绿，再谈配置细节。

```bash
cd ~/work/py/ai-agent/week05-codebase-agent-homework
npm install                 # 首次
npm run cli -- tasks        # 看有哪几类任务
npm run cli -- agent "巡检技术债" --task debt_audit --decider auto
npm run cli -- trace-ui --port 4317
```

浏览器打开 `http://127.0.0.1:4317/`，第一条就是刚跑完的对话：

- 总运行时长 **177 ms**，5 轮，5 次模型调用，4 次工具调用，全部绿色。
- 最长的一条是第 4 轮的 `git_status`，**103 ms**，占总时长 58%。

这条链路不连模型、不连审批图，只用 `config/scripts/debt-audit.yaml` 里的离线脚本，所以可复现、可反复跑。

---

## 1. 环境配置

### 1.1 硬性要求

| 项 | 要求 | 说明 |
|---|---|---|
| Node.js | ≥ 22（实测 v25.2.1） | `node:sqlite` 是内置模块，低于 22 没有 |
| npm | ≥ 10（实测 11.6.2） | 装依赖、跑脚本 |
| 平台 | macOS / Linux / WSL | Windows 原生未验证 |
| 磁盘 | 约 500 MB | `node_modules` 为主 |

自检：

```bash
node -v && npm -v
npm run check     # tsc 类型检查，应无输出
npm test          # 25 个测试文件 / 160 个用例，应 0 failed
```

依赖已在 `package.json` 里声明：`@earendil-works/pi-agent-core`、`pi-ai`、`tsx`、`vitest`、`typescript`、`yaml`。不需要全局装任何 CLI。

### 1.2 目录结构（知道东西放哪）

```
week05-codebase-agent-homework/
├── config/
│   ├── platform.yaml        # 模型接入、预算、HITL、权限、沙箱
│   ├── tasks.yaml           # 任务类型：工作区、交付物、审批门口
│   └── scripts/             # 离线决策脚本（*每步的文本与工具调用*）
├── samples/
│   ├── 1_login_demo/        # 任务一、任务三的仓库
│   └── 2_bug_fix/demo-app/  # 任务二的仓库
├── data/platform.db         # SQLite：runs / approvals / audit / trace_spans
├── src/                     # Agent、运行时、治理、轨迹采集
└── docs/                    # 说明文档
```

两处路径容易搞错：

- `config/platform.yaml` 里的 `runtime.database: ../data/platform.db` 相对**配置文件所在目录**解析，最终落在项目根的 `data/`。
- `tasks.yaml` 里的 `workspace` 相对 `config/` 解析，所以写的是 `../samples/...`。

### 1.3 模型接入：三种档位，先想清楚你要哪种

| 档位 | 需要的配置 | 决策是谁做的 | 适合 |
|---|---|---|---|
| **离线脚本** | 无 | `config/scripts/*.yaml` 里的固定步骤 | 验证轨迹图、复现、CI |
| **本地 Gateway** | `GATEWAY_API_KEY` + 本机 `ai-agent/app` 在 `:8000` | 你的 Gateway，逻辑模型 `chat-default` | 想复用自己网关的路由与计费 |
| **DeepSeek 直连** | `DEEPSEEK_API_KEY` + `--provider deepseek` | DeepSeek 原生 API | 不想起网关，只要真模型 |

环境变量（全部可选，不设就用 `config/platform.yaml` 的默认值）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `PLATFORM_CONFIG` | 换一份平台配置 | `config/platform.yaml` |
| `MODEL_PROVIDER` | `gateway` / `deepseek` | `gateway` |
| `GATEWAY_BASE_URL` | Gateway 地址 | `http://127.0.0.1:8000/v1` |
| `GATEWAY_MODEL_ID` | Gateway 上的逻辑模型名 | `chat-default` |
| `GATEWAY_API_KEY` | Gateway 凭据 | — |
| `DEEPSEEK_BASE_URL` | DeepSeek 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL_ID` | 模型名 | `deepseek-v4-flash` |
| `DEEPSEEK_API_KEY` | DeepSeek 凭据 | — |
| `HITL_MODE` | `disabled` / `http`，**优先级高于 tasks.yaml** | 取配置 |
| `HITL_BASE_URL` | 审批图地址 | `http://127.0.0.1:8383` |
| `HITL_API_KEY` | 审批图凭据 | — |

凭据只放环境变量或本机 `.env`，不要写进仓库；日志与摘要里一律显示为 `[REDACTED]`。

**跑真实模型：**

```bash
# 方式 A：本地 Gateway（先确保 app 已启动）
export GATEWAY_API_KEY=<your-key>
curl -s http://127.0.0.1:8000/v1/models | head -c 200   # 能返回列表再继续
npm run cli -- agent "把登录逻辑写成说明文档"

# 方式 B：DeepSeek 直连
export DEEPSEEK_API_KEY=<your-key>
npm run cli -- agent "巡检技术债" --task debt_audit --provider deepseek
```

启动后第一行会打印决策器身份，这是判断"到底谁在决策"的唯一依据：

```
[决策器] real:deepseek/deepseek-v4-flash · DEEPSEEK_API_KEY 已设置，连接 https://api.deepseek.com
[决策器] real:ai-agent-gateway/chat-default · GATEWAY_API_KEY 已设置，连接 http://127.0.0.1:8000/v1
[决策器] mock:debt-audit.yaml · 离线脚本决策器（debt-audit.yaml）
```

`real:` = 真模型，`mock:` = 离线脚本。

### 1.4 决策口径与降级规则

`--decider` 三档，规则写在 `src/pi/decider.ts`，值得记住：

| 口径 | 行为 |
|---|---|
| `real`（CLI 默认） | 有对应 provider 的 key 就连真模型；没 key 时**降到任务条目里配的脚本**，并打印"降级"；连脚本都没有就报 `MODEL_UNAVAILABLE` |
| `auto` | 有 `--script` 或任务配了脚本就离线；否则有 key 就连真模型 |
| `mock` | 只准离线；没有脚本报 `SCRIPT_REQUIRED` |

`real` 只认**当前 provider 的**凭据变量：`--provider deepseek` 看 `DEEPSEEK_API_KEY`，默认 gateway 看 `GATEWAY_API_KEY`。用错变量会被判成"没钥匙"而静默降级到脚本——所以每次先看那行 `real:` / `mock:`。

### 1.5 配置文件导读（只讲会改到的段）

`config/platform.yaml`：

- `gateway` / `deepseek`：两个 provider 的地址、模型名、凭据变量名。`model.provider` 决定默认选哪个。
- `model`：上下文窗口 128k、输出上限 4096、每百万 token 单价（用于成本估算）。
- `runtime`：单次运行上限——8 轮、12 次工具、重复调用 2 次、3 万 token、$0.25、120 秒；`context` 段控制上下文压缩（保留最近 2 轮、审计摘要 30 行）。
- `hitl`：审批默认档位、名单、超时。
- `policy`：角色权限与工具白名单。默认角色 `maintainer` 才能写文件、执行命令。
- `sandbox`：工作区根、禁读（`.env`、`*.pem`、`*.key`）、禁写（`package.json`、`.git` 等）、命令超时 20 秒。

`config/tasks.yaml`：三类任务，每类定义工作区、交付物、预算、审批方式。

| 任务 | 关键词 | 交付物 | 审批门口 |
|---|---|---|---|
| `understand` | 登录逻辑 / 调用链 / 说明文档 | `artifacts/login-flow.md` | `gate: auto`、`via: http` |
| `fix_tests` | 修复 / 失败用例 / 缺陷 | `src/auth/session-policy.ts` | 只拦 `run_test`、`via: http` |
| `debt_audit` | 技术债 / 巡检 | `docs/tech-debt-audit.md` | `gate: none`、`via: local` |

关键字路由：命令里的 prompt 命中 `match` 就领对应任务；也可以用 `--task <id>` 点名，或用 `--no-route` 不领任务。

### 1.6 审批环境：四档，决定轨迹图上有没有"人"

| 档位 | 怎么来 | 写文件时发生什么 | 轨迹图上的样子 |
|---|---|---|---|
| **无人值守** | 任务 `approval.gate: none` | 直接写，审计记 `APPROVAL_GATE_OFF` | 无审批条 |
| **本地审批** | 任务 `approval.via: local` | 落一张 `approvals` 单，等 `approvals decide` | 审批门槛条 + 人工等待条 |
| **审批图** | `via: http` 或 `HITL_MODE=http` | 请求 `:8383` 的 LangGraph 服务，需 `HITL_API_KEY` | 同上，等待时长 = 图的处理时间 |
| **自动审批** | 任意档位 + `--auto-approve <身份>` | 单子照开，由这个身份当场签字，然后执行 | `自动审批 · write_file` 条，详情里 `auto: true`、`decided_by` 是那个身份 |

任务条目里的 `via` 优先级高于 `platform.yaml` 的 `hitl.mode`（见 `src/tasks.ts`）。所以默认配置下 `understand` 会去连审批图，而图没起、key 没配时，运行停在 `HITL_KEY_MISSING`——这是**配置结果**，不是程序坏了。

本地审批的完整闭环：

```bash
npm run cli -- agent "巡检技术债" --task debt_audit --decider auto   # 若 via: local 且 gate 拦了
npm run cli -- approvals list --status pending
npm run cli -- approvals decide <approval-id> --decision approve --actor maintainer
npm run cli -- agent --resume <run-id>
```

`approve` 只授权这一次冻结动作；`reject` 保存拒绝并暂停，之后要再跑必须显式 `--resume`。

#### 自动审批：让流程自己走完

```bash
# 本地落单：不问图，单子由指定身份当场批掉。understand 这类 via: http 的任务也能跑通。
HITL_MODE=disabled npm run cli -- agent "把登录逻辑写成说明文档" \
  --decider auto --script config/scripts/understand-login.yaml \
  --auto-approve maintainer-auto
```

也可以不写命令行，改用环境变量：`export AUTO_APPROVE_ACTOR=maintainer-auto`。

**它到底省了什么、没省什么**：

- 省的是"等人按键"：门槛照旧判定，`approvals` 表照旧开单，`args_digest` 照旧算，动作照旧一次性领取，`runs` 照旧落盘。
- 没省的是审计：审计表多一条 `code: AUTO_APPROVED`，`hitl.approved` 事件带 `auto: true`，轨迹图上写的是"自动审批"而不是"人工审批"。
- 不归它管的是硬拒绝：`PATH_DENY_WRITE`、契约违规、预算超限这些在门口就拦下，不会因为加了 `--auto-approve` 就放行。
- 用在真实仓库前想清楚：它把"谁能批"收敛成一个名字。演示、CI、练手仓库合适；生产写操作仍应保留人工或图上的审批规则。身份名要能一眼看出是机器（如 `ci-auto-approver`），别用真人名字。

**两种通道怎么选**：

| 你想验的 | 命令 | 前提 |
|---|---|---|
| 离线跑完整流程（推荐入门/CI） | `HITL_MODE=disabled ... --auto-approve <身份>` | 无。单子落在本地 SQLite |
| 验审批图链路也能自动走 | `HITL_MODE=http ... --auto-approve <身份>` | `:8383` 服务在跑 + `HITL_API_KEY` 已设 |

第二条路的语义是"替人答图上的工单"：运行时照旧先提单，图给出 `awaiting_human` 时再用这个身份调 `/resume` 批准。图不可达或 key 缺失时**不会**偷偷改成本地批准，而是带着 `HITL_KEY_MISSING` / `HITL_UNREACHABLE` 停下，错误信息里会提示改用 `HITL_MODE=disabled`。

### 1.7 首次自检清单

```bash
node -v                                   # >= 22
npm run check                             # 无输出
npm test                                  # 160 passed（含 4 条自动审批用例）
npm run cli -- tasks | head -20           # 三类任务及工作区
npm run cli -- tools | head -20           # 模型可见的工具
npm run cli -- contract | head -40        # 工具契约：字段与边界
```

`tools` / `contract` 只是把模型视角摊开给你看，不产生轨迹数据。

---

## 2. 真实开始运行

### 2.1 一次运行会发生什么

1. 按 prompt 领任务（或 `--task` 点名），解析出工作区、交付物、审批门口。
2. 选决策器（真模型或离线脚本），打印 `[决策器] ...`。
3. 进入 Agent Loop：每轮 = 一次模型调用 + 若干工具调用，受预算约束。
4. 每次写操作过审批门口，按档位等人或直接放行。
5. 收尾时核对交付物、测试退出码，写 `runs` 与 `trace_spans`。
6. 全过程每个阶段都有一条 span：`run → turn → model/tool/approval`。

### 2.2 场景一：跑一条全绿的对话（推荐入门）

```bash
npm run cli -- agent "巡检技术债" --task debt_audit --decider auto
```

终端会依次出现：任务路由行、决策器行、每轮工具行、交付物行。末行是运行摘要 JSON，关键字段：

```json
{ "task": "debt_audit", "stop": "COMPLETED", "acceptance": "passed", "missing": [] }
```

实测这条：5 轮、5 次模型调用、4 次工具调用、总时长 177 ms，交付物 `samples/1_login_demo/docs/tech-debt-audit.md` 写入成功。

### 2.3 场景二：跑一条被审批拦下的对话（看红色怎么来的）

```bash
npm run cli -- agent "把登录逻辑写成说明文档" --decider auto
```

期望结果：`stop: HITL_KEY_MISSING`、`acceptance: failed`、`missing: [交付物 artifacts/login-flow.md 没有成功写入证据]`。

原因链：`understand` 任务配了 `via: http` → 写文档时请求 `http://127.0.0.1:8383` → 没起图服务、没配 `HITL_API_KEY` → 门槛直接拒绝，模型没机会继续。

这条红色轨迹恰好是分析素材：图上能看到"模型想写 → 被门槛拦 → 运行收尾"。

### 2.4 场景三：换真模型跑同一条任务

```bash
export DEEPSEEK_API_KEY=<your-key>
npm run cli -- agent "巡检技术债" --task debt_audit --provider deepseek
```

第一行必须是 `real:deepseek/deepseek-v4-flash`。如果出现 `mock:` 或"降级"字样，说明 key 没被读到（变量名、当前 shell、`--provider` 三者之一不对）。

真模型和脚本的差异会直接体现在轨迹图上：模型条从几毫秒变成几百毫秒到数秒，还可能出现更多轮次、参数更"自由"的工具调用。这正是对比分析的价值。

### 2.5 场景四：自动审批跑通一次完整对话

```bash
HITL_MODE=disabled npm run cli -- agent "把登录逻辑写成说明文档" \
  --decider auto --script config/scripts/understand-login.yaml \
  --auto-approve maintainer-auto
```

实测结果（`trace c2b804c6-0ef7-492b-b3d1-ef83cc2c0fa5`）：

- 7 轮、7 次模型调用、9 次工具调用、总时长 159 ms（模型 30 ms / 工具 123 ms / 审批等待 2 ms）。
- 终端出现一行 `自动审批：单号 … · 工具 write_file · 由 maintainer-auto 立即批准`。
- 收尾 `stop: COMPLETED`、`acceptance: passed`、`missing: []`；交付物 `samples/1_login_demo/artifacts/login-flow.md` 真的写出来了（1562 字节）。
- 验收证据里 `approved_by` 是 `maintainer-auto`，不是 `null`——这条能对上账，是因为自动审批和人工批准共用同一个 `hitl.approved` 事件，只多了 `auto: true`。

对照 2.3 的场景二：同一个任务、同一个脚本，只多一个 `HITL_MODE=disabled` 和 `--auto-approve`，结果从 `HITL_KEY_MISSING` 变成全绿。轨迹图上多一条"自动审批 · write_file"（2 ms），少了原来那条挂起的红条。

### 2.6 拿 `trace_id`

三种方式，任选：

1. 运行结束时终端摘要里有 `trace_id`。
2. `npm run cli -- runs list`，取最新一条的 `trace_id`（与 `run_id` 不是一回事：一次 run 一个 trace，resume 复用同一 trace）。
3. 直接查库：

```bash
node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("data/platform.db");
console.table(db.prepare(`
  SELECT trace_id, COUNT(*) spans, MAX(end_ms)-MIN(start_ms) total_ms
  FROM trace_spans GROUP BY trace_id ORDER BY MIN(start_ms) DESC LIMIT 5
`).all());
'
```

---

## 3. 打开轨迹图

```bash
npm run cli -- trace-ui --port 4317
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 4317 | 被占用会报 `PORT_INVALID` 或 `EADDRINUSE`，换一个 |
| `--host` | 127.0.0.1 | 只监听本机 |
| `--trace-id` | 最近一条 | 直接定位某次对话 |
| `--open` | 关 | 启动后调系统浏览器 |

地址两种写法：

- `http://127.0.0.1:4317/` —— 默认最近一次对话，可用下拉框切换。
- `http://127.0.0.1:4317/?trace=<trace_id>` —— 固定某一次，方便贴给别人。

健康检查：`curl -s http://127.0.0.1:4317/healthz` 返回 `{"ok":true}`。

服务只做两件事：把 `src/trace/ui.html` 发出去，把 `collector` 的结果序列化成 JSON。**它启动时读一次 HTML，改界面后必须重启。**

---

## 4. 界面怎么读

自上而下四个区：

1. **统计卡**：总运行时长/起始钟点、轮次（平均每轮耗时）、模型调用（合计耗时）、工具调用（合计耗时）、审批门槛（人工等待次数与时长）、异常数、结束码、验收结果。
2. **时间构成条**：一条横向堆叠条，按颜色比例显示模型 / 工具 / 审批 / 空档各占多少。
3. **甘特时间轴**：左侧标签列（类型徽标 + 名称；第二行 `开始时刻 · 持续时长`），右侧条形按真实墙钟绘制。
4. **详情面板**：点任意一行，右侧显示该调用的完整字段；再点一次或点空白处收起。

读图四条硬规则：

- **条长 = 真实墙钟耗时**。不是事件顺序估算，也不是 token 数折算。
- **颜色 = 类型**：紫=运行、灰=轮次、蓝=模型、绿=工具、橙=审批、红=失败/被拦。
- **缩进 = 父子关系**：`run → turn → model/tool/approval`；轮次的灰条是**后代包络**，一定完整包住该轮所有调用。
- **刻度和缩放**：刻度按总时长自动分档；滑块 1–8× 只改视觉宽度，不改数据。

`turn` 的灰条可能比"该轮模型调用 + 工具调用"更长，因为它画的是从第一个子调用开始到最后一个子调用结束的区间，包含调度与上下文渲染时间。看单次调用耗时请看子条与详情。

---

## 5. 分析动作（配真实数字）

### 5.1 先看总账：时间花在哪

打开 `43763b8a-fbeb-4f77-820f-a8fdf5c8bbe2`：

- 总时长 177 ms；模型 5 次共 40 ms（23%）；工具 4 次共 123 ms（69%）；轮次合计 170 ms，剩余 7 ms 是收尾开销。
- 结论：**瓶颈在工具，不在模型**。优化方向是工具执行，不是换更快的模型。

判断阈值（经验值）：工具占比 > 60% 先查工具；模型占比 > 70% 先查提示词长度与轮次；审批等待 > 30% 先查审批链路。

### 5.2 找最慢的一次调用

按条长排序，或直接查：

```bash
node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("data/platform.db");
const rows = db.prepare("SELECT kind, name, turn_index, duration_ms FROM trace_spans WHERE trace_id = ? ORDER BY duration_ms DESC").all(process.argv[1]);
console.table(rows.filter(r => r.kind === "tool" || r.kind === "model").slice(0, 8));
' 43763b8a-fbeb-4f77-820f-a8fdf5c8bbe2
```

实测：`git_status` 103 ms 排第一，是第二名 `search_code`（12 ms）的 8 倍，占整条对话 58%。

这一步的价值不在"哪个慢"，而在追问"为什么慢"：`git_status` 首次执行要 spawn 进程、扫工作树；如果它反复出现在每条对话里，就值得在运行时做进程复用或缓存。**轨迹图给证据，优化决策仍然是人做。**

### 5.3 看轮次节奏

- 每轮一条灰条。本例 5 轮耗时为 `24 / 10 / 21 / 109 / 6 ms`。
- 第 4 轮 109 ms 明显突出 → 点开看到它是 `git_status` 独占，模型只花了 4 ms。
- 如果某一轮"模型条很长、工具条很短"，通常是上下文变长导致模型推理变慢，或该轮在生成长文本。

### 5.4 看模型调用细节

点模型行，详情里有：模型 ID、决策器（`managed-script/decider` 或 `deepseek/deepseek-v4-flash`）、输入/输出/缓存 token、成本估算、停止原因（`stop` / `toolUse` / `length`）、输出文本预览。

两个高频判断：

- `停止原因 = length` → 输出被 `max_tokens` 截断，回答不完整。
- 输入 token 逐轮增长很快 → 上下文压缩没生效，检查 `runtime.context.keep_recent_turns` 与审计摘要配置。

### 5.5 看异常与审批

红色条有三类，含义不同，别混：

| 形态 | 含义 | 处理 |
|---|---|---|
| `run` 红 | 整次运行失败/未达验收 | 看详情「结束码」「缺失项」 |
| `tool` 红 | 工具执行失败 | 看错误块里的 code 与 stderr |
| 橙条 `审批门槛` + 状态 `blocked` | 门口拒绝了这个动作，动作没执行 | 看「是否执行：否」「单据状态」 |

审批相关的两个统计是分开的：**审批门槛**是"过了几道门"，**人工等待**是"真的有人花了多久"。脚本/离线跑时门槛可能 1 道、人工等待 0 次——因为系统预检就拒了，没到人。

以 `1ef5324c-29e5-42a4-b61d-d7009772ccb2` 为例：6 轮、9 次工具、1 道门槛、异常 3、结束码 `HITL_KEY_MISSING`；审批单状态 `denied`，决策人显示「—（系统预检失败，未到人）」。这就是"配置导致的拒绝"，不是模型行为问题。

### 5.6 对比两次运行

导出两份 JSON（界面右上「导出」，或 `curl -s .../api/trace/<id> > a.json`），再比：

```bash
node -e '
const a = require("./a.json"), b = require("./b.json");
const row = (t) => ({
  total: t.totalMs, turns: t.stats.turns,
  model: t.stats.modelCalls, tool: t.stats.toolCalls,
  modelMs: t.stats.modelMs, toolMs: t.stats.toolMs,
  errors: t.stats.errors, stop: t.stats.stop,
});
console.table({ 脚本: row(a), 真模型: row(b) });
'
```

对比时只比"同类任务、同类决策器"才有意义。脚本 vs 真模型的差异说明模型开销；两次真模型的差异才说明提示词或代码改动的影响。

### 5.7 详情字段速查

| 行类型 | 关键字段 |
|---|---|
| 对话（根） | 标题、任务、工作区、模型、决策器、结束码、验收、缺失项、token/成本合计 |
| 轮次 | 序号、开始/结束、持续、该轮工具次数、区间包络起止 |
| 模型 | 模型 ID、决策器、tokens（输入/输出/缓存/总计）、成本、停止原因、输出预览 |
| 工具 | 工具名、参数、是否执行、结果摘要/错误码、`tool_call_id` |
| 审批 | 人工还是自动、审批单号、单据状态、决策人（`decided_by`）、决策时间、消费时间、参数摘要、等待时长 |

---

## 6. 排障

### 6.1 运行期错误码

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `MODEL_UNAVAILABLE` | `real` 模式没 key，且任务没配脚本 | 设对应 provider 的 key，或加 `--script`，或用 `--decider auto` |
| `HITL_KEY_MISSING` / `HITL_UNREACHABLE` | 自动审批想答审批图，但图没起或 key 没配 | 起 `:8383` + 设 `HITL_API_KEY`；离线就加 `HITL_MODE=disabled` |
| `SCRIPT_REQUIRED` | `mock`/`auto` 但没有可用脚本 | 指 `--script`，或给任务补 `script` |
| `DECIDER_MODE` | `--decider` 写了别的值 | 只能是 `auto`/`mock`/`real` |
| `MODEL_PROVIDER_UNKNOWN` | `--provider` 不是 gateway/deepseek | 改对拼写 |
| `HITL_KEY_MISSING` | 走了 http 审批但没有 `HITL_API_KEY` 或图不可达 | 起审批图并配 key，或把任务改成 `via: local`/`gate: none` |
| `RUN_TASK_MISMATCH` | `--resume` 时任务与原始运行不一致 | 用原任务的配置恢复 |
| `TEST_FAILED` | `run_test` 退出码非 0 | 看工具结果里的测试输出 |
| `PORT_INVALID` / `EADDRINUSE` | 端口非法或被占用 | 换端口，或 `pkill -f "cli.ts trace-ui"` |

### 6.2 轨迹图不显示

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面空白 | 服务没起 / 端口错 | `curl /healthz` 先确认 |
| 下拉框为空 | 库里没有 span | 先跑一次对话，再点刷新 |
| 改了 HTML 没变化 | 服务启动时只读一次 | 重启 `trace-ui` + 浏览器硬刷新 |
| 条都挤在左边几像素 | 脚本决策器是毫秒级 | 正常，4–8× 缩放看 |
| 时间显示 1970 | span 的 `start_ms` 缺失 | 查该行数据库记录，确认埋点是否绕过 `SpanRecorder` |
| 列表数字与详情不一致 | 有人另写了一套聚合 | 列表必须复用 `collectTrace` |

### 6.3 清理与重置

```bash
pkill -f "cli.ts trace-ui"                      # 停服务
lsof -i :4317 || echo "4317 free"               # 确认端口

cp data/platform.db data/platform.db.bak        # 备份
node -e 'const {DatabaseSync}=require("node:sqlite");const db=new DatabaseSync("data/platform.db");
db.prepare("DELETE FROM trace_spans WHERE trace_id = ?").run(process.argv[1]);' <trace-id>   # 只删一条对话

git checkout -- samples                          # 还原示例仓库里被改的文件
git clean -fd samples/1_login_demo/docs          # 清掉运行生成的报告
```

`data/platform.db` 里同时存 `runs`、`approvals`、`audit`、`trace_spans`。删 `trace_spans` 只影响轨迹图，不影响运行记录。

### 6.4 安全清单

- 凭据只通过环境变量注入，不写进 `config/*.yaml`、脚本、日志、截图。
- 工作区写操作受 `sandbox` 约束：`deny_read` 挡 `.env`/`*.pem`/`*.key`，`deny_write` 挡 `package.json`、`.git`。
- 角色决定权限：默认 `maintainer` 才有 `repo:write`/`repo:execute`；只读场景传 `--roles reviewer`。
- 轨迹图只监听 `127.0.0.1`，不要改成 `0.0.0.0` 暴露到局域网。

---

## 7. 一页速查

```bash
# 环境
node -v && npm install && npm run check && npm test

# 跑对话
npm run cli -- agent "巡检技术债" --task debt_audit --decider auto          # 离线，全绿
npm run cli -- agent "把登录逻辑写成说明文档" --decider auto                 # 会被审批拦，红色样本
HITL_MODE=disabled npm run cli -- agent "把登录逻辑写成说明文档" --decider auto \
  --auto-approve maintainer-auto                                           # 自动审批，全绿
npm run cli -- agent "巡检技术债" --provider deepseek                       # 真模型
npm run cli -- agent --resume <run-id>                                      # 恢复

# 看轨迹
npm run cli -- trace-ui --port 4317
open http://127.0.0.1:4317/?trace=<trace-id>
curl -s http://127.0.0.1:4317/api/trace/<trace-id> | head -c 400

# 审批
npm run cli -- approvals list --status pending
npm run cli -- approvals decide <id> --decision approve --actor maintainer
```