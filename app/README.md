# AI Agent Service — 使用说明

基于 FastAPI + SSE 的 Agent 服务，支持流式对话、工具调用和多轮会话。

## 快速启动

```bash
cd ~/work/py/ai-agent

# 激活环境
conda activate ai-agent

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

服务默认监听 `http://localhost:8000`。

---

## 接口总览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/runs` | 创建新会话 / 继续已有会话 |
| GET | `/v1/runs/{run_id}/stream` | 订阅 SSE 事件流 |
| POST | `/v1/runs/{run_id}/cancel` | 取消正在执行的 Run |
| GET | `/v1/runs/{run_id}` | 获取 Run 状态详情 |
| GET | `/v1/runs/{run_id}/messages` | 获取完整对话历史 |
| GET | `/v1/runs` | 列出最近的 Run |
| GET | `/health` | 健康检查 |

---

## 核心流程

```
客户端                                       服务端
  │                                            │
  │─── POST /v1/runs ───────────────────────►  │ 创建 Run + 启动 agent loop
  │◄── {"run_id":"...", "last_event_id":1} ──  │
  │                                            │
  │─── GET /v1/runs/{id}/stream?last_event_id=1 ►│ 订阅 SSE 流
  │◄── event: text.delta ────────────────────  │
  │◄── event: text.delta ────────────────────  │
  │◄── event: text.done ─────────────────────  │
  │◄── event: run.completed ─────────────────  │ 流结束
  │                                            │
  │─── POST /v1/runs (带 run_id) ────────────► │ 继续对话
  │◄── {"run_id":"...", "last_event_id":22} ── │
  │                                            │
  │─── GET /v1/runs/{id}/stream?last_event_id=22 ►│
  │◄── ... ──────────────────────────────────  │
```

---

## 1. 创建新会话

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "你好，我叫小明"}],
    "system": "你是一个友好的助手。",
    "model": "deepseek-v4-pro",
    "temperature": 0.7
  }'
```

**响应：**
```json
{
  "run_id": "f1dafded-132d-4452-b9ad-29be95871f08",
  "status": "created",
  "last_event_id": 1
}
```

---

## 2. 订阅 SSE 事件流

拿到 `run_id` 后订阅事件流，`last_event_id` 传创建时返回的值（跳过 `run.created` 事件）：

```bash
curl -N http://localhost:8000/v1/runs/{run_id}/stream?last_event_id=1
```

**输出示例：**
```
id: 2
event: run.in_progress
data: {"event":"run.in_progress","run_id":"f1dafded-...","data":{"turn":1},"timestamp":1724234567.89,"sequence":2}

id: 3
event: text.delta
data: {"event":"text.delta","run_id":"f1dafded-...","data":{"content":"你好"},"timestamp":1724234567.91,"sequence":3}

id: 4
event: text.delta
data: {"event":"text.delta","run_id":"f1dafded-...","data":{"content":"，小明！"},"timestamp":1724234567.92,"sequence":4}

id: 5
event: text.done
data: {"event":"text.done","run_id":"f1dafded-...","data":{"content":"你好，小明！很高兴认识你。"},"timestamp":1724234567.95,"sequence":5}

id: 6
event: run.completed
data: {"event":"run.completed","run_id":"f1dafded-...","data":{"total_turns":1},"timestamp":1724234568.01,"sequence":6}
```

---

## 3. 继续对话（多轮）

带上之前的 `run_id`，只需发新的用户消息。服务端自动追加到对话历史：

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "f1dafded-132d-4452-b9ad-29be95871f08",
    "messages": [{"role": "user", "content": "我叫什么名字？"}]
  }'
```

**响应：**
```json
{
  "run_id": "f1dafded-132d-4452-b9ad-29be95871f08",
  "status": "created",
  "last_event_id": 7
}
```

然后再次订阅流：
```bash
curl -N http://localhost:8000/v1/runs/{run_id}/stream?last_event_id=7
```

模型会记住上下文回答"你叫小明"。

---

## 4. 带工具调用

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
    "tools": [
      {
        "name": "get_weather",
        "description": "获取指定城市的天气信息",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名称"}
          },
          "required": ["city"]
        }
      }
    ]
  }'
```

**SSE 事件流会额外包含工具调用事件：**
```
event: tool.calling
data: {..., "data":{"tool_call_id":"call_xxx","name":"get_weather","arguments":{"city":"北京"}}}

event: tool.result
data: {..., "data":{"tool_call_id":"call_xxx","name":"get_weather","result":"{\"city\":\"北京\",\"weather\":\"晴\",\"temperature\":28}"}}
```

模型收到工具结果后会自动生成最终回复。

---

## 5. 取消 Run

```bash
curl -X POST http://localhost:8000/v1/runs/{run_id}/cancel
```

**响应：**
```json
{
  "run_id": "f1dafded-...",
  "status": "cancelled",
  "message": "Run cancelled successfully"
}
```

---

## 6. 查看 Run 状态

```bash
curl http://localhost:8000/v1/runs/{run_id}
```

**响应：**
```json
{
  "run_id": "f1dafded-...",
  "status": "completed",
  "created_at": 1724234567.12,
  "completed_at": 1724234568.01,
  "model": "deepseek-v4-pro",
  "total_turns": 1,
  "error": null
}
```

---

## 7. 查看对话历史

```bash
curl http://localhost:8000/v1/runs/{run_id}/messages
```

**响应：**
```json
{
  "run_id": "f1dafded-...",
  "messages": [
    {"role": "system", "content": "你是一个友好的助手。"},
    {"role": "user", "content": "你好，我叫小明"},
    {"role": "assistant", "content": "你好，小明！很高兴认识你。"},
    {"role": "user", "content": "我叫什么名字？"},
    {"role": "assistant", "content": "你叫小明。"}
  ]
}
```

---

## 8. 列出所有 Run

```bash
curl http://localhost:8000/v1/runs?limit=10
```

---

## 9. 健康检查

```bash
curl http://localhost:8000/health
```

**响应：**
```json
{"status": "ok"}
```

---

## SSE 事件类型参考

| 事件类型 | 含义 | data 字段 |
|----------|------|-----------|
| `run.created` | Run 已创建 | `{model}` |
| `run.in_progress` | 开始新一轮 LLM 调用 | `{turn}` |
| `text.delta` | 文本流式片段 | `{content}` |
| `text.done` | 本轮文本输出完毕 | `{content}` (完整文本) |
| `tool.calling` | 模型决定调用工具 | `{tool_call_id, name, arguments}` |
| `tool.result` | 工具执行结果 | `{tool_call_id, name, result}` |
| `run.completed` | Run 正常结束 | `{total_turns}` |
| `run.failed` | Run 执行失败 | `{error}` |
| `run.cancelled` | Run 被取消 | `{}` |

---

## 客户端接入要点

### 断线重连

订阅 SSE 时传 `last_event_id` 参数（上次收到的最后一个 sequence）：

```bash
curl -N http://localhost:8000/v1/runs/{run_id}/stream?last_event_id=15
```

服务端只推送 sequence > 15 的事件。

### 多轮对话流程

```
1. POST /v1/runs (不带 run_id) → 新建会话，保存返回的 run_id
2. GET  /v1/runs/{id}/stream?last_event_id=N → 收到回复
3. POST /v1/runs (带 run_id + 新 user message) → 继续对话
4. GET  /v1/runs/{id}/stream?last_event_id=M → 收到回复
5. 重复 3-4...
```

### 并发保护

同一个 Run 不能同时执行两个 agent loop。如果上一轮还没结束就发新消息，会收到 409 错误：

```json
{"detail": "Run xxx is still in progress, wait for completion or cancel first"}
```

---

## 环境变量（.env）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API 密钥 | (必填) |
| `LLM_BASE_URL` | API 基地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 默认模型 | `gpt-4o-mini` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

---

## 项目结构

```
app/
├── main.py                  # FastAPI 入口
├── core/
│   ├── config.py            # 配置管理
│   └── logging_config.py    # 结构化日志
├── models/
│   ├── events.py            # RunEvent / EventType
│   ├── schemas.py           # API 请求/响应模型
│   └── streaming.py         # StreamingModel 抽象基类
├── adapters/
│   └── openai_adapter.py    # OpenAI 兼容 API 实现
├── services/
│   ├── agent_loop.py        # Agent Loop 核心引擎
│   └── run_store.py         # Run 状态 + 对话历史存储
└── api/
    └── routes.py            # HTTP 路由
```
