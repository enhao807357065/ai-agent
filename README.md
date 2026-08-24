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

> 更多 curl 示例请参考 [docs/curl-examples.md](docs/curl-examples.md)

---

## 项目结构

```
ai-agent/
├── app/                        # Agent 服务端（FastAPI）
│   ├── main.py                 # 应用入口 & 生命周期管理
│   ├── api/
│   │   └── routes.py           # HTTP 路由（Run CRUD、SSE 订阅）
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
│   │   ├── schemas.py          # Pydantic 请求/响应 DTO
│   │   └── streaming.py        # 流式模型抽象接口
│   └── services/
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
| 结构化输出 | Pydantic V2 |
| 日志 | structlog (JSON) |
| 限流 | 滑动窗口（内存实现，RPM/TPM 双维度） |
| 重试 | tenacity |
| 模板引擎 | Jinja2 |
| 运行时 | Python 3.12 / asyncio |
