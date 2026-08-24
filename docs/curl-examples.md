# API Curl 请求示例

## 基础地址

```
BASE_URL=http://localhost:8000/v1
```

---

## 1. 创建 Run — 流式（默认）

```bash
# 第一步：创建 Run（stream=true 为默认）
curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "用一句话解释什么是Agent"}],
    "model": "deepseek-v4-pro",
    "system": "你是一个有帮助的AI助手。",
    "temperature": 0.7,
    "stream": true
  }'

# 响应示例：
# {"run_id": "abc-123", "status": "created", "last_event_id": 1}

# 第二步：订阅 SSE 事件流
curl -s -N "$BASE_URL/runs/abc-123/stream?last_event_id=1"

# SSE 事件格式：
# data: {"event": "run.in_progress", "data": {"turn": 1}, "sequence": 2}
# data: {"event": "text.delta", "data": {"content": "Agent", "turn": 1}, "sequence": 3}
# data: {"event": "text.delta", "data": {"content": "是一个", "turn": 1}, "sequence": 4}
# data: {"event": "text.done", "data": {"content": "Agent是一个...", "turn": 1}, "sequence": 5}
# data: {"event": "run.completed", "data": {"final_text": "...", "total_turns": 1}, "sequence": 6}
```

---

## 2. 创建 Run — 非流式

```bash
# stream=false 时，服务端同步等待完整结果后返回
curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "用一句话解释什么是Agent"}],
    "model": "deepseek-v4-pro",
    "system": "你是一个有帮助的AI助手。",
    "temperature": 0.7,
    "stream": false
  }' | python3 -m json.tool

# 响应示例：
# {
#   "run_id": "abc-456",
#   "status": "completed",
#   "message": {
#     "role": "assistant",
#     "content": "Agent是一个能感知环境、自主决策并执行动作以达成目标的智能系统。",
#     "tool_calls": null
#   },
#   "total_turns": 1
# }
```

---

## 3. 继续对话（多轮）

```bash
# 在已有 run_id 上追加消息
curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "abc-456",
    "messages": [{"role": "user", "content": "再详细解释一下ReAct模式"}],
    "stream": false
  }' | python3 -m json.tool
```

---

## 4. 带工具调用

```bash
curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
    "model": "deepseek-v4-pro",
    "system": "你是一个有帮助的AI助手，可以查询天气。",
    "tools": [
      {
        "name": "get_weather",
        "description": "获取指定城市的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名"}
          },
          "required": ["city"]
        }
      }
    ],
    "stream": false
  }' | python3 -m json.tool

# 响应示例（agent loop 会自动执行工具并继续）：
# {
#   "run_id": "abc-789",
#   "status": "completed",
#   "message": {
#     "role": "assistant",
#     "content": "北京今天的天气是晴天，温度28°C。"
#   },
#   "total_turns": 2
# }
```

---

## 5. 获取 Run 状态

```bash
curl -s "$BASE_URL/runs/abc-123" | python3 -m json.tool
```

---

## 6. 获取完整对话历史

```bash
curl -s "$BASE_URL/runs/abc-123/messages" | python3 -m json.tool
```

---

## 7. 取消运行中的 Run

```bash
curl -s -X POST "$BASE_URL/runs/abc-123/cancel" | python3 -m json.tool
```

---

## 8. 列出最近的 Run

```bash
curl -s "$BASE_URL/runs?limit=10" | python3 -m json.tool
```

---

## 9. 断线重连（SSE）

```bash
# 如果连接断开，用 last_event_id 恢复（不会丢事件）
curl -s -N "$BASE_URL/runs/abc-123/stream?last_event_id=42"
```

---

## 10. 健康检查

```bash
curl -s $BASE_URL/../health
# 或
curl -s http://localhost:8000/health
```

---

## 流式 vs 非流式对比

| 特性 | `stream: true`（默认） | `stream: false` |
|------|----------------------|-----------------|
| POST /runs 响应 | 立即返回 run_id | 等待执行完毕后返回完整结果 |
| 获取内容方式 | 订阅 SSE 事件流 | 直接从响应中获取 |
| 适用场景 | Web UI 逐字展示 | CLI 工具 / API 集成 |
| 多轮工具调用 | SSE 推送每步进度 | 阻塞等待全部完成 |
| 超时风险 | 低（长连接） | 高（复杂任务可能超时） |
