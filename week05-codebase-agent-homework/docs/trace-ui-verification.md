# 对话轨迹图（trace-ui）验证操作说明

适用项目：`~/work/py/ai-agent/week05-codebase-agent-homework`
验证目标：证明「一次完整对话」的执行过程可以被图形化展示，并逐项核对总时长、轮次、每次调用的开始时间、持续时长与内容摘要。

验证分六步，全程只读数据库与本地 HTTP 服务，不改代码、不写仓库文件。除步骤 1 会真实跑一次 Agent 外，其余步骤都是观察与比对。

---

## 0. 前置条件

| 项 | 要求 | 自检命令 |
|---|---|---|
| Node.js | ≥ 22（需要 `node:sqlite`） | `node -v` |
| 依赖 | 已安装 `node_modules` | `ls node_modules/.bin/tsx` |
| 工作目录 | 项目根目录 | `cd ~/work/py/ai-agent/week05-codebase-agent-homework` |
| 端口 | 4317 未被占用 | `lsof -i :4317` |

自检（全部通过再继续）：

```bash
cd ~/work/py/ai-agent/week05-codebase-agent-homework
node -v
npm run check
npm test
```

期望：`check` 无输出且退出码 0；`test` 输出 `Test Files 25 passed`、`Tests 160 passed`（数字随版本变化，关键是 0 failed）。

---

## 1. 跑一次完整对话，产生轨迹数据

轨迹图的数据来自每次运行写入的 `trace_spans` 表。库里已有历史对话时可跳过本步；要验证"新对话能被采到"，必须跑一次。

```bash
npm run cli -- agent "把登录逻辑写成说明文档" \
  --decider auto \
  --script config/scripts/understand-login.yaml
```

参数含义：

- `--decider auto`：有离线脚本就用脚本，不调用外部模型，结果可复现。
- `--script config/scripts/understand-login.yaml`：本次对话的固定决策序列（6 轮）。

期望现象：

1. 命令退出码为 0 或 1，终端打印最终回答与验收结果。
2. 末次运行以 `HITL_KEY_MISSING`、`验收 未通过` 收尾。这是**预期**结果：`config/platform.yaml` 里 `hitl.mode: disabled`，写文件动作被审批门槛拦下；它恰好提供了一条「被拦截」样本，不是轨迹图故障。

记录本次 `trace_id`（一条对话一个）：

```bash
node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("data/platform.db");
console.table(db.prepare(`
  SELECT trace_id, COUNT(*) AS spans, MIN(start_ms) AS first_ms
  FROM trace_spans GROUP BY trace_id ORDER BY first_ms DESC LIMIT 5
`).all());
'
```

期望：第一行就是刚跑完的对话，`spans` 为 20 以上（本次实测 24 条原始 span，去重后界面显示 23 行）。把 `trace_id` 复制下来，后文记作 `<TRACE_ID>`。

---

## 2. 启动轨迹图服务

```bash
npm run cli -- trace-ui --port 4317
```

期望输出（时间、ID 与本次不同属正常）：

```
轨迹图已启动：http://127.0.0.1:4317/
可选对话（最新在前）：
  <trace-id>  6 轮  把登录逻辑写成说明文档
Ctrl+C 停止服务。
```

健康检查（另开一个终端）：

```bash
curl -s http://127.0.0.1:4317/healthz
```

期望：`{"ok":true}`。

> 服务把 HTML 读进内存一次。**每次改 `src/trace/ui.html` 后必须重启服务**，否则浏览器看到的是旧页面。
> 服务未启动或端口写错时，后面所有 `curl` 都会失败，先解决这一层。

---

## 3. 页面验收：五条硬指标

浏览器打开：

```
http://127.0.0.1:4317/?trace=<TRACE_ID>
```

不带 `?trace=` 时默认展示最近一次对话。

### 3.1 总运行时长

看左上第一张卡片「总运行时长」。

- 期望：一个可读时长（如 `185 ms` 或 `2.4 s`），副行给出起始钟点（如 `17:56:36.380 起`）。
- 交叉验证：页面底部状态栏的「总时长」必须与卡片一致。

### 3.2 轮次

看第二张卡片「轮次」。脚本决策的登录场景应为 `6`，副行为「平均 X/轮」。

- 图中每一轮有一条灰色区间条，标注「第 N 轮」，右侧列出该轮的工具次数。
- 灰色区间条必须完整包住该轮下的模型条和工具条；若灰条明显短于子条，是包络计算错误。

### 3.3 每次调用的开始时间

看每一行左侧标签列的**第二行**：`HH:MM:SS.mmm · 时长`。

- 任意两行比较：模型调用应紧接在该轮开始之后，工具调用应晚于同轮模型调用。
- 第一轮第一行的开始时间应等于总时长的起始钟点（允许 0–1 ms 的取整差）。

### 3.4 每次调用的持续时长

- 左侧第二行的数值即该 span 的真实墙钟耗时。
- 条形长度与时长成正比：同一横坐标尺度下，`124 ms` 的条应明显长于 `3 ms` 的条。
- 点击任意一行，右侧详情的「持续时长」必须等于左侧第二行的数值。

### 3.5 每次调用的内容摘要

逐类型抽检，点击行看右侧详情：

| 类型 | 必看字段 | 期望 |
|---|---|---|
| 模型 | 模型、决策器、输入/输出/总计 tokens、停止原因 | 数值齐全；脚本决策器为 `mock:understand-login.yaml` |
| 模型 | 模型输出 | 一段文本预览，不是空 |
| 工具 | 工具名、调用参数、结果摘要 | `search_code` 的参数含 `pattern=login`；`read_file` 含文件路径 |
| 工具 | 是否执行 | 被拦的 `write_file` 显示「否（被门口拦下）」 |
| 审批 | 审批单、单据状态、决策人、参数摘要 | 审批单是 UUID；状态 `denied`；决策人「—（系统预检失败，未到人）」 |
| 对话（根行） | 目标、任务、工作区、结束码、验收、缺失项 | 结束码 `HITL_KEY_MISSING`；验收「未通过」 |

---

## 4. 交互验收

按顺序操作，每步都要有可见变化：

1. **点击行**：右侧详情从空状态变为该调用的完整信息；被选中行左侧出现蓝色竖线。
2. **筛选**：点「只看模型」，只剩根行、轮次行与模型条；点「只看异常」，只剩根行与 2 条红色条（`write_file` 与 `write_file · 审批门槛`）。
3. **缩放**：拖动滑块到 2×，横向出现滚动条，条形等比拉长，刻度同步变化；拉回 1× 恢复。
4. **导出**：点「导出」，下载 `trace-<trace_id>.json`；用编辑器打开应包含 `traceId / stats / nodes` 三个顶层字段。
5. **实时**：勾选「实时」，在另一个终端再跑步骤 1 的命令；当前对话若仍在写入，条形应增长，页面每 2 秒刷新一次且不打断滚动位置。没有新数据时页面不重绘。
6. **切换对话**：下拉框列出最近对话（时间 · 标题 · 总时长），切换后统计卡与时间轴整体替换。

---

## 5. 与数据库、API 交叉验证

页面不能自证。用同一份数据在三个地方对账：数据库 → API → 页面。

### 5.1 直接查 `trace_spans`

```bash
node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("data/platform.db");
const traceId = process.argv[1];
console.table(db.prepare(`
  SELECT kind, status, COUNT(*) AS n,
         SUM(duration_ms) AS total_ms,
         MIN(start_ms) AS first_ms, MAX(end_ms) AS last_ms
  FROM trace_spans WHERE trace_id = ?
  GROUP BY kind, status ORDER BY kind
`).all(traceId));
console.log("总时长(ms) =", db.prepare(
  "SELECT MAX(COALESCE(end_ms,start_ms)) - MIN(start_ms) AS ms FROM trace_spans WHERE trace_id = ?"
).get(traceId).ms);
' <TRACE_ID>
```

期望（示例对话 `1ef5324c…` 的实测值；换一次运行数值会变，比对规则不变）：

| kind | status | n | total_ms |
|---|---|---|---|
| approval | blocked | 1 | 1 |
| model | ok | 6 | 26 |
| run | error | 1 | 185 |
| tool | blocked | 1 | 0 |
| tool | error | 1 | 3 |
| tool | ok | 8 | 149 |
| turn | ok | 6 | 179 |

比对规则：

- 页面「模型调用 6」= `model` 行计数；「合计 26 ms」= 其 `total_ms`。
- 页面「工具调用 9」= `tool` 的 8 条 ok + 1 条 error；那条 `blocked`（0 ms）与 error 共享同一个 `tool_call_id`，被 collector 去重，**不计入也不显示**。
- 页面「异常 3」= `run error` + `tool error` + `approval blocked`。
- 页面「审批门槛 1」= `approval` 行计数。
- 页面「总运行时长」= 上面打印的 `总时长(ms)`，允许 1 ms 取整差。
- 页面「轮次 6 · 合计 179 ms」= `turn` 行计数与 `total_ms`。

### 5.2 查 API

```bash
curl -s http://127.0.0.1:4317/api/runs | node -e '
let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
  const { runs } = JSON.parse(s);
  console.table(runs.map(r => ({ trace: r.traceId.slice(0,8), title: r.title, totalMs: r.totalMs, turns: r.stats.turns, errors: r.stats.errors })));
});'
curl -s http://127.0.0.1:4317/api/trace/<TRACE_ID> | node -e '
let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
  const t = JSON.parse(s);
  console.log("nodes", t.nodes.length, "spanCount", t.stats.spanCount, "totalMs", t.totalMs);
  console.log(t.nodes.filter(n => n.kind === "approval").map(n => n.payload.approval_id));
});'
```

期望：

- `nodes.length` 等于 `stats.spanCount`，等于界面底部「N 行」。
- 审批 span 的 `payload.approval_id` 是 UUID（由 collector 从 `approvals` 表回填），不是 `undefined`。
- `totalMs` 与页面一致。
- `/api/runs` 中该对话的 `errors`、`toolCalls` 与 `/api/trace/:id` 相同：列表复用详情同一个 collector，两处不一致就说明有人另写了一套聚合。

### 5.3 对账判据

三处任一不一致，以数据库为准，按下面顺序定位：

1. 数据库与 API 不一致 → `src/trace/collector.ts` 的拼装/去重逻辑。
2. API 与页面不一致 → `src/trace/ui.html` 的渲染或缓存（先重启服务再刷新）。

---

## 6. 边界场景（可选，但建议至少验证一条）

| 场景 | 构造方式 | 图上应看到 |
|---|---|---|
| 被拦截的调用 | 步骤 1 默认就会产生（`write_file` 命中审批门槛） | 一条红色条纹条，状态「被拦截」，详情「是否执行：否」 |
| 未执行但不该消失 | 同上：`trace_spans` 中有 `executed:false` 的 tool 行 | 同一 `tool_call_id` 只出现一行，不与执行失败行重复 |
| 人工等待 | `HITL_MODE=http` 启动 Agent 并保持 pending 一段时间后批准 | 一条橙色斜纹条「人工审批 · X」，时长 = 审批单 `created_at → decided_at` |
| 自动审批 | `HITL_MODE=disabled npm run cli -- agent "把登录逻辑写成说明文档" --decider auto --auto-approve maintainer-auto` | 一条绿色「自动审批 · write_file」条（数毫秒），详情 `auto: true`、`decided_by: maintainer-auto`；验收证据 `approved_by` 不为 `null` |
| 跨进程恢复 | `agent --resume <run-id>` | 同一 `trace_id`，时间轴上出现第二段 `run` 区间；轮次连续编号，不重置 |
| 旧会话无 span | 选择采集上线前产生的 trace | 顶部黄色提示条"该对话产生于轨迹采集上线之前"，不画空图 |

---

## 7. 验收清单

对照作业要求逐条打勾：

- [ ] 图形化界面可打开，展示的是**一次完整对话**（单条 `trace_id`）
- [ ] 总运行时长可见，且与数据库/API 一致
- [ ] 轮次可见，每轮有区间条并完整包住子调用
- [ ] 每次调用都有开始时间（绝对钟点 + 相对偏移）
- [ ] 每次调用都有持续时长，且条形长度与时长成正比
- [ ] 每次调用都有内容摘要（模型文本/工具参数/审批单/错误）
- [ ] 调用类型可见（对话/轮次/模型/工具/审批）
- [ ] 调用状态可见（成功/失败/被拦截/等待/已批准）
- [ ] 父子关系可见（缩进 + 详情「父调用」）
- [ ] 错误信息可见（红色条 + 详情错误块）
- [ ] 自动审批的条目标为「自动审批」，且 `decided_by` 是执行时给的身份（不是 `null`）
- [ ] 模型名、工具名可见
- [ ] `npm run check` 与 `npm test` 均通过

---

## 8. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `EADDRINUSE` | 4317 被占用 | 换端口：`trace-ui --port 4318`，或 `pkill -f "cli.ts trace-ui"` |
| 页面一直空白 | 服务未启动 / 端口不符 | 先 `curl /healthz`，再检查 URL |
| 下拉框为空 | 库里没有 span | 执行步骤 1，再点「刷新」 |
| 改了界面没变化 | 服务启动时读取 HTML 一次 | 重启 `trace-ui`，浏览器硬刷新 |
| 所有条挤在左侧几像素 | 脚本决策器耗时是毫秒级 | 正常；用 4–8× 缩放查看，或改用真实模型决策 |
| 时间显示成 1970 | `start_ms` 缺失/为 0 | 查该 span 的 `start_ms`，检查埋点是否绕过 `SpanRecorder` |
| 「人工等待 0 次」但有审批单 | 审批单在门槛阶段就被系统拒绝，没有真正等人 | 正常；卡片区分「审批门槛」与「人工等待」 |
| 结束码 `HITL_KEY_MISSING` | 任务 `via: http`，审批图没起或没配 `HITL_API_KEY` | 起 `:8383` + 设 key；离线验收加 `HITL_MODE=disabled` |
| 加了 `--auto-approve` 仍停在 `HITL_KEY_MISSING` | 任务是 `via: http`，自动身份先去答图，图不可达 | 离线跑请带 `HITL_MODE=disabled`（本地落单），这是有意为之：图在时不许绕过图 |

---

## 9. 收尾

```bash
# 停止服务：前台按 Ctrl+C；后台启动则
pkill -f "cli.ts trace-ui"

# 确认端口释放
lsof -i :4317 || echo "4317 free"
```

验证过程中不改仓库内容；`data/platform.db` 会因步骤 1 增加一条对话记录，属正常数据增长。
