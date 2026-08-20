# AI Agent 学习项目

从零构建 AI Agent 的实践项目，覆盖 LLM 调用、Agent 循环、流式服务、结构化输出等核心能力。

## 项目结构

```
ai-agent/
├── .env                    # 环境变量（LLM API Key，不入库）
├── week01/                 # 第一周：Agent 基础能力
│   ├── chat_responses.py   # OpenAI Chat/Responses API 基础调用
│   ├── 1-1/               # Agent Loop 基础
│   │   ├── agnet_loop_demo.py  # 完整 Agent 循环（工具调用 + 沙箱执行）
│   │   └── sandbox_runner.py   # 代码沙箱运行器
│   ├── 1-2/               # 模型适配器 + Mini Agent
│   │   ├── model_adapter.py    # 抽象基类（ModelAdapter）
│   │   ├── ds_adapter.py       # DeepSeek 适配器实现
│   │   ├── openai_adapter.py   # OpenAI Responses API 适配器
│   │   ├── py_pydantic.py      # Pydantic 结构化输出定义
│   │   ├── mini_loop.py        # 精简版 Agent Loop
│   │   └── temp_and_topp.py    # Temperature 与 Top-P 采样实验
│   ├── 1-3/               # 流式服务（SSE）
│   │   ├── streaming.py       # FastAPI SSE 服务端（支持断开/重连/缓存）
│   │   └── index.html         # 前端页面（EventSource 断开重连演示）
│   ├── 1-4/               # Prompt 模板引擎
│   │   └── type_render.py     # Jinja2 模板渲染 + 指纹校验
│   └── 1-5/               # 结构化输出与字段校验
│       ├── field_rule.py           # Pydantic model_validator 字段规则
│       └── openai_responses_parse.py  # responses.parse 结构化输出实践
```

## 环境要求

- Python 3.12+
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
pip install openai python-dotenv pydantic jinja2 fastapi uvicorn

# 配置 API Key
cp .env.example .env
# 编辑 .env 填入你的 LLM_API_KEY
```

## 各模块说明

### 1-1 Agent Loop

基础 Agent 循环实现：用户输入 → LLM 决策 → 工具调用 → 观察结果 → 循环直到完成。

### 1-2 模型适配器

抽象适配器模式封装不同 LLM 后端，支持 DeepSeek（Chat Completions）和 OpenAI（Responses API）两种风格。

### 1-3 流式服务

FastAPI SSE 流式推理服务，核心特性：
- 后台 asyncio.Task 生成，前端断开不影响后端
- 服务端内存缓存已生成内容
- 支持 EventSource 重连（`Last-Event-ID` 续约）

### 1-4 Prompt 模板

基于 Jinja2 的 Prompt 工程实践：`StrictUndefined` 防遗漏、信任边界标注、SHA256 指纹追踪。

### 1-5 结构化输出

Pydantic `model_validator` 实现 Agent 决策的字段约束校验，配合 `responses.parse` 一步获得类型安全的结构化结果。

## 技术栈

| 组件 | 选型 |
|------|------|
| LLM | DeepSeek V4 Flash (via TAL 网关) |
| SDK | OpenAI Python SDK |
| Web 框架 | FastAPI + Uvicorn |
| 结构化输出 | Pydantic V2 |
| 模板引擎 | Jinja2 |
| 运行时 | Python 3.12 / asyncio |
