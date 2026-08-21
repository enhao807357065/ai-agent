# AI Agent 学习项目

从零构建 AI Agent 的实践项目，覆盖 LLM 调用、Agent 循环、流式服务、结构化输出、持久化等核心能力。

## 项目结构

```
ai-agent/
├── app/                        # Agent 服务端（FastAPI）
│   ├── main.py                 # 应用入口 & 生命周期管理
│   ├── api/
│   │   └── routes.py           # HTTP 路由（Run CRUD、SSE 订阅）
│   ├── adapters/
│   │   └── openai_adapter.py   # OpenAI Chat Completions 适配器
│   ├── core/
│   │   ├── config.py           # 配置管理（环境变量 + .env）
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
│       └── run_store.py        # 内存运行时状态管理（SSE 事件流）
├── week01/                     # 第一周：Agent 基础能力
│   ├── chat_responses.py       # OpenAI Chat/Responses API 基础调用
│   ├── 1-1/                    # Agent Loop 基础
│   │   ├── agnet_loop_demo.py  # 完整 Agent 循环（工具调用 + 沙箱执行）
│   │   └── sandbox_runner.py   # 代码沙箱运行器
│   ├── 1-2/                    # 模型适配器 + Mini Agent
│   │   ├── model_adapter.py    # 抽象基类（ModelAdapter）
│   │   ├── ds_adapter.py       # DeepSeek 适配器实现
│   │   ├── openai_adapter.py   # OpenAI Responses API 适配器
│   │   ├── py_pydantic.py      # Pydantic 结构化输出定义
│   │   ├── mini_loop.py        # 精简版 Agent Loop
│   │   └── temp_and_topp.py    # Temperature 与 Top-P 采样实验
│   ├── 1-3/                    # 流式服务（SSE）
│   │   ├── streaming.py        # FastAPI SSE 服务端（断开/重连/缓存）
│   │   └── index.html          # 前端页面（EventSource 断开重连演示）
│   ├── 1-4/                    # Prompt 模板引擎
│   │   └── type_render.py      # Jinja2 模板渲染 + 指纹校验
│   └── 1-5/                    # 结构化输出与字段校验
│       ├── field_rule.py       # Pydantic model_validator 字段规则
│       └── openai_responses_parse.py  # responses.parse 结构化输出实践
├── .env                        # 环境变量（不入库）
├── .env.example                # 环境变量示例
├── requirements.txt            # Python 依赖
└── .gitignore
```

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

### API 接口

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
| GET | `/health` | 健康检查 |

## 环境要求

- Python 3.12+
- MySQL 8.0+
- Conda 环境：`ai-agent`

## 快速开始

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
# 编辑 .env 填入你的配置

# 创建数据库
mysql -u root -p -e "CREATE DATABASE ai_agent DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 启动服务（自动建表）
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 测试
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hello"}]}'
```

## 各模块说明

### app/ — Agent 服务端

完整的 Agent-as-a-Service 实现：
- **Agent Loop**：支持多轮工具调用、指数退避重试、结构化日志、checkpoint 持久化
- **SSE 流式输出**：实时推送 text delta、工具调用、执行结果
- **断点恢复**：服务重启后从 MySQL checkpoint 恢复 Run 继续执行
- **多轮对话**：同一 run_id 可追加消息，保持上下文

### week01/ — Agent 基础能力

| 模块 | 内容 |
|------|------|
| 1-1 | Agent Loop 基础：用户输入 → LLM 决策 → 工具调用 → 循环 |
| 1-2 | 适配器模式封装多 LLM 后端（DeepSeek / OpenAI） |
| 1-3 | FastAPI SSE 流式推理 + EventSource 断开重连 |
| 1-4 | Jinja2 Prompt 模板：StrictUndefined + SHA256 指纹 |
| 1-5 | Pydantic model_validator 字段约束 + responses.parse |

## 技术栈

| 组件 | 选型 |
|------|------|
| LLM | DeepSeek V4 (via OpenAI 兼容网关) |
| SDK | OpenAI Python SDK |
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | MySQL 8.0 + SQLAlchemy 2.0 (async) |
| DB 驱动 | aiomysql |
| 结构化输出 | Pydantic V2 |
| 日志 | structlog (JSON) |
| 重试 | tenacity |
| 模板引擎 | Jinja2 |
| 运行时 | Python 3.12 / asyncio |
