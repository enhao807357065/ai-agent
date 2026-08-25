# AI Agent 学习项目

从零构建 AI Agent 的实践项目，覆盖 LLM 调用、Agent 循环、流式服务、结构化输出、持久化等核心能力。

## 快速开始

### 环境准备

```bash
# 克隆项目
git clone https://github.com/enhao807357065/ai-agent.git
cd ai-agent

# 创建环境
conda create -n ai-agent python=3.12 -y
conda activate ai-agent

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Key 和数据库配置（参考 .env.example 中的注释）
```

### 数据库初始化

```bash
# MySQL 8.0+，创建数据库
mysql -u root -p -e "CREATE DATABASE ai_agent DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

### 启动服务

```bash
# 启动（自动建表）
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 验证服务启动成功
curl -s http://localhost:8000/health
# → {"status": "healthy", ...}
```

### 快速验证

**命令行验证：**

```bash
BASE_URL=http://localhost:8000/v1

# 非流式调用（最简单的验证方式）
curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "用一句话解释什么是Agent"}],
    "stream": false
  }' | python3 -m json.tool

# 流式调用（两步：创建 → 订阅 SSE）
RUN_ID=$(curl -s -X POST $BASE_URL/runs \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hello"}]}' | python3 -c "import sys,json;print(json.load(sys.stdin)['run_id'])")

curl -s -N "$BASE_URL/runs/$RUN_ID/stream?last_event_id=1"
```

**网页端验证：**

浏览器打开 http://localhost:8000 即可进入 Web UI，支持：
- 实时流式对话（逐字输出）
- 底部 Settings 栏切换 System Prompt 版本（通用助手 / 编程助手 / Agent 架构师）
- 多轮会话管理（左侧 Session 列表）

> 更多 curl 示例请参考 [docs/curl-examples.md](docs/curl-examples.md)

### 作为统一 LLM Gateway 使用

Gateway 对外提供一套**厂商无关的 Pydantic HTTP/SSE 契约**。以下三个历史路径是同一个 Gateway 契约的路径别名，**不再分别模拟 OpenAI Chat / Responses / Anthropic 的响应格式**：

```text
POST /v1/chat/completions
POST /v1/responses
POST /v1/messages
GET  /v1/models
```

调用方只传逻辑模型名，例如 `chat-default`；不会传入或看到真实的 `provider / upstream model`。Gateway 内部由 Adapter 封装 TAL AI、DeepSeek Anthropic API、DeepSeek Responses API 等协议差异。

#### 非流式调用

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chat-default",
    "messages": [
      {"role": "user", "content": "用一句话解释什么是 LLM Gateway"}
    ],
    "stream": false,
    "max_tokens": 256
  }' | python3 -m json.tool
```

成功时返回统一结构：

```json
{
  "id": "gwresp_...",
  "object": "gateway.response",
  "created": 0,
  "model": "chat-default",
  "content": "...",
  "tool_calls": [],
  "finish_reason": "stop",
  "usage": {"input_tokens": 0, "output_tokens": 0}
}
```

#### 流式调用

```bash
curl -sS -N http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chat-default",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true,
    "max_tokens": 256
  }'
```

SSE `data:` 内是 Gateway 自有事件：

```text
gateway.text.delta
gateway.tool_call.completed
gateway.completed
gateway.error
```

> 客户端必须以 `gateway.completed` 作为成功完成信号；收到 `gateway.error` 时，当前响应失败，不能将此前的增量内容当作可持久化或可执行的最终结果。

---

### Gateway 路由与故障转移

Gateway 的路由依据是**能力匹配、业务优先级、成本和健康状态**，不是模型名称、参数量或“看起来最强”。

```text
GatewayRequest
  → 从 stream / tools / response_format 提取能力需求
  → Routing Policy 限定当前 logical model 的候选 target 池
  → Target Registry 过滤能力、Token 限制、开关、预算、Circuit Open target
  → priority → estimated_cost → health 字典序排序
  → 调用最优候选；仅对可恢复错误尝试下一个候选
```

`.env` 中两类配置职责不同：

| 配置 | 描述 |
|---|---|
| `GATEWAY_TARGET_REGISTRY` | 真实目标的 provider、模型、能力、优先级、价格、Token 限制与启用状态 |
| `GATEWAY_ROUTING_POLICIES` | 逻辑模型可以使用哪些 target、额外能力约束、候选排序方式与预算上限 |

当前支持的核心 capability：

```text
chat / streaming / tool_calling / json_object / json_schema / reasoning
```

例如请求携带 `tools` 和 `stream=true` 时，只会选择同时声明 `tool_calling`、`streaming` 的 target。`json_schema` 请求只会选择声明 `json_schema` 的 target。

可恢复的 `ConnectionError`、超时、429、5xx 会记录 target 失败；连续失败达到 `GATEWAY_CIRCUIT_FAILURE_THRESHOLD`（默认 3）后，该 target 进入 Circuit Open 状态 `GATEWAY_CIRCUIT_OPEN_SECONDS`（默认 60 秒），不再参与候选选择。成功调用会恢复健康状态。

流式请求仅在**第一个文本或工具调用事件之前**允许切换候选；一旦输出已经开始，绝不 fallback，避免重复或混杂内容。

---

### Structured Output

#### `json_object`

`json_object` 仅要求模型返回一个 JSON object，不约束字段名、必填字段、类型或嵌套结构。建议在 prompt 中明确要求 JSON：

```json
{
  "response_format": {"type": "json_object"}
}
```

#### `json_schema`

`json_schema` 同时要求：

1. 候选 target 声明 `json_schema` capability；
2. Gateway 在请求上游前校验调用方提供的 JSON Schema；
3. 上游完成后，Gateway 对完整 `content` 执行 `json.loads` 与 Draft 2020-12 JSON Schema 校验。

示例：

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chat-default",
    "messages": [
      {"role": "system", "content": "Return only JSON matching the requested schema."},
      {"role": "user", "content": "介绍你自己"}
    ],
    "max_tokens": 256,
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "assistant_identity",
        "schema": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "name": {"type": "string"},
            "is_ai": {"type": "boolean"}
          },
          "required": ["name", "is_ai"]
        }
      }
    }
  }' | python3 -m json.tool
```

| 场景 | 非流式语义 | 流式语义 |
|---|---|---|
| 调用方 Schema 非法 | `422 structured_output_schema_invalid`，不会请求上游 | 同左，SSE 启动前返回 HTTP 422 |
| 上游输出不是 JSON / 不符合 Schema | `502 structured_output_invalid` | 先前的 delta **可能已经发送**；末尾发送 `gateway.error`，不会发送 `gateway.completed` |

因此，对于需要严格机器执行、落库或触发外部副作用的 `json_schema` 请求，推荐：

```text
stream=false
```

如果采用 `stream=true`，客户端应先缓存 `gateway.text.delta`，只在收到 `gateway.completed` 后再解析、落库或执行；收到 `gateway.error` 时必须丢弃缓存。

---

### Gateway 错误契约

非流式错误统一为：

```json
{
  "object": "gateway.error",
  "error": {
    "code": "...",
    "message": "...",
    "retryable": false
  }
}
```

常见错误码：

| HTTP | code | 含义 |
|---:|---|---|
| 400 | `invalid_model` | 逻辑模型不存在或路由策略无效 |
| 422 | `invalid_request` | Gateway 请求字段或 JSON body 校验失败 |
| 422 | `capability_unavailable` | 没有 target 满足本次能力要求 |
| 422 | `structured_output_schema_invalid` | 调用方提供的 JSON Schema 非法 |
| 500 | `gateway_configuration_error` | 网关自身的 provider/凭证/连接配置错误 |
| 502 | `structured_output_invalid` | 上游完整输出未满足要求的 JSON Schema |
| 503 | `upstream_unavailable` | 所有能力兼容 target 当前不可用 |

公开错误不会泄露 API key、上游 endpoint、provider 原始异常或真实模型名；这些信息仅进入服务端结构化日志。

---

## 项目结构

```
ai-agent/
├── app/                        # Agent 服务端（FastAPI）
│   ├── main.py                 # 应用入口 & 生命周期管理
│   ├── api/
│   │   └── gateway.py          # 统一 LLM Gateway HTTP/SSE 路由
│   ├── adapters/
│   │   ├── __init__.py         # Adapter 工厂（根据 LLM_PROVIDER 创建模型）
│   │   ├── talai_adapter.py    # TAL AI 网关适配器（OpenAI Chat 兼容格式）
│   │   ├── deepseek_adapter.py # DeepSeek Anthropic API 适配器
│   │   └── deepseek_responses_adapter.py  # DeepSeek Responses API 适配器
│   ├── core/
│   │   ├── config.py           # 配置管理（环境变量 + .env）
│   │   ├── system_prompts.py   # System Prompt 版本管理（Jinja2 模板）
│   │   ├── database.py         # SQLAlchemy async engine & session
│   │   └── logging_config.py   # structlog 结构化日志配置
│   ├── models/
│   │   ├── db_models.py        # ORM 模型（runs/messages/checkpoints）
│   │   ├── events.py           # SSE 事件协议定义
│   │   ├── schemas.py          # Agent 请求/响应 DTO
│   │   ├── gateway.py          # Gateway HTTP/SSE 契约
│   │   ├── capabilities.py     # Target 能力、路由策略与健康领域模型
│   │   └── streaming.py        # 流式模型抽象接口
│   └── services/
│       ├── gateway_model_router.py  # 能力兼容候选调用与受限 fallback
│       ├── gateway_candidate_selector.py # 能力/成本/健康候选选择
│       ├── target_health_registry.py # 进程内 Circuit Breaker
│       ├── gateway_structured_output_validator.py # JSON Schema 本地校验
│       ├── agent_loop.py       # Agent Loop 核心引擎（重试、日志、checkpoint）
│       ├── db_service.py       # 数据库持久化 CRUD
│       ├── rate_limiter.py     # 滑动窗口限流器（RPM/TPM）
│       └── run_store.py        # 内存运行时状态管理（SSE 事件流）
├── week01/                     # 第一周：Agent 基础能力
│   ├── chat_responses.py       # OpenAI Chat/Responses API 基础调用
│   ├── 1-1/                    # Agent Loop 基础
│   ├── 1-2/                    # 模型适配器 + Mini Agent
│   ├── 1-3/                    # 流式服务（SSE）
│   ├── 1-4/                    # Prompt 模板引擎
│   └── 1-5/                    # 结构化输出与字段校验
├── docs/
│   └── curl-examples.md        # 完整 API curl 请求示例
├── .env.example                # 环境变量模板
├── requirements.txt            # Python 依赖
└── .gitignore
```

---

## 架构设计

### 核心流程

```
Client → POST /v1/runs → 创建 RunState（内存） + 持久化（MySQL）
                        → 启动 asyncio.Task 执行 Agent Loop
                        → 返回 run_id

Client → GET /v1/runs/{id}/stream → SSE 订阅事件流
                                   → text.delta / tool.calling / tool.result / run.completed

Agent Loop（每轮 Turn）:
    1. 调用 LLM（流式，支持指数退避重试）
    2. 若有 tool_calls → 执行工具 → 结果追加到 messages
    3. 保存 checkpoint 到 MySQL
    4. 若 finish_reason=stop → 结束
```

### 分层设计

| 层 | 目录 | 职责 |
|----|------|------|
| Router | `app/api/` | HTTP 路由、参数校验、SSE 推流 |
| DTO | `app/models/schemas.py` | 请求/响应数据结构（Pydantic） |
| Service | `app/services/` | 业务逻辑（Agent Loop、运行时状态） |
| Repository | `app/services/db_service.py` | 数据库 CRUD |
| Model | `app/models/db_models.py` | ORM 表定义 |
| Adapter | `app/adapters/` | 外部 LLM 服务封装 |
| Core | `app/core/` | 配置、日志、数据库连接等基础设施 |

### 持久化策略

- **内存 RunStore**：管活跃 Run 的实时状态（SSE 事件流、Task 句柄、流式 delta）
- **MySQL**：管历史记录（Run/Messages/Checkpoints），服务重启后可恢复
- **读取策略**：内存优先，fallback DB
- **设计原则**：不使用数据库外键约束，关联关系仅在 ORM 层声明

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/runs` | 创建 Run（支持续对话） |
| GET | `/v1/runs/{id}/stream` | SSE 事件流订阅 |
| GET | `/v1/runs/{id}` | 获取 Run 详情 |
| GET | `/v1/runs/{id}/messages` | 获取完整对话历史 |
| GET | `/v1/runs/{id}/checkpoints` | 获取检查点列表 |
| POST | `/v1/runs/{id}/cancel` | 取消 Run |
| POST | `/v1/runs/{id}/resume` | 从 DB 恢复并继续执行 |
| GET | `/v1/runs` | 列出最近的 Run |
| GET | `/v1/system-prompts` | 获取 System Prompt 版本列表 |
| GET | `/v1/system-prompts/{id}` | 获取指定 Prompt 详情 |
| POST | `/v1/system-prompts/{id}/render` | Jinja2 渲染指定 Prompt |
| GET | `/v1/rate-limits` | 查询限流状态 |
| GET | `/v1/models` | Gateway 逻辑模型列表 |
| POST | `/v1/chat/completions` | 统一 Gateway 调用路径别名 |
| POST | `/v1/responses` | 统一 Gateway 调用路径别名 |
| POST | `/v1/messages` | 统一 Gateway 调用路径别名 |
| GET | `/health` | 健康检查 |

---

## 配置说明

通过 `.env` 文件配置，支持 3 种 LLM Provider 切换：

```bash
# Provider 选择（三选一）
LLM_PROVIDER=talai              # TAL AI 网关（OpenAI Chat 兼容）
LLM_PROVIDER=deepseek           # DeepSeek Anthropic API
LLM_PROVIDER=deepseek_responses # DeepSeek Responses API
```

详细配置项参考 [.env.example](.env.example)。

---

## 各模块说明

### app/ — Agent 服务端

完整的 Agent-as-a-Service 实现：
- **Agent Loop**：支持多轮工具调用、指数退避重试、结构化日志、checkpoint 持久化
- **SSE 流式输出**：实时推送 text delta、工具调用、执行结果
- **断点恢复**：服务重启后从 MySQL checkpoint 恢复 Run 继续执行
- **多轮对话**：同一 run_id 可追加消息，保持上下文
- **限流保护**：滑动窗口 RPM/TPM 限流，超限返回 429
- **System Prompt 管理**：Jinja2 模板引擎，支持变量注入和版本切换

### week01/ — Agent 基础能力

| 模块 | 内容 |
|------|------|
| 1-1 | Agent Loop 基础：用户输入 → LLM 决策 → 工具调用 → 循环 |
| 1-2 | 适配器模式封装多 LLM 后端（DeepSeek / OpenAI） |
| 1-3 | FastAPI SSE 流式推理 + EventSource 断开重连 |
| 1-4 | Jinja2 Prompt 模板：StrictUndefined + SHA256 指纹 |
| 1-5 | Pydantic model_validator 字段约束 + responses.parse |

---

## 技术栈

| 组件 | 选型 |
|------|------|
| LLM | DeepSeek V4 (via OpenAI 兼容网关 / Anthropic API / Responses API) |
| SDK | OpenAI Python SDK + Anthropic Python SDK |
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | MySQL 8.0 + SQLAlchemy 2.0 (async) |
| DB 驱动 | aiomysql |
| 结构化输出 | Pydantic V2 + jsonschema (Draft 2020-12) |
| 日志 | structlog (JSON) |
| 限流 | 滑动窗口（内存实现，RPM/TPM 双维度） |
| 重试 | tenacity |
| 模板引擎 | Jinja2 |
| 运行时 | Python 3.12 / asyncio |
