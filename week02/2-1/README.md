# 2-1：Mini Agent Loop + Tool Runtime

这个练习将 `search_orders` 工具、受控 `ToolRuntime` 与 LLM 的 function calling 串成一个最小可运行 Agent。

目标不是让模型直接访问业务对象，而是让模型只生成 Tool Call；Runtime 决定工具是否可见、参数是否有效、是否允许执行，以及如何把结果安全地写回模型上下文。

## 执行流程

```text
用户问题
  ↓
Mini Agent Loop：计算当前可见工具
  ↓
LLM：返回普通文本，或返回 tool_calls
  ↓
ToolRuntime：查找 → 参数校验 → 权限/审批校验 → timeout/retry → 输出校验 → trace
  ↓
ToolResultMessage：以 role=tool 写回 messages
  ↓
LLM：基于工具结果生成最终回答，或继续发起下一轮 Tool Call
```

`mini_agent_loop.py` 使用最多 5 轮的上限，防止模型连续调用工具导致无限循环。

## 文件职责

| 文件 | 职责 |
|---|---|
| `mini_agent_loop.py` | Agent 主循环：调用 LLM、处理 Tool Call、写回 Tool Result、控制最大轮数 |
| `tool_runtime.py` | 不可信 Tool Call 的受控执行入口：校验、鉴权、审批、超时、重试、输出验证、trace |
| `tool_definition.py` | 工具声明契约：输入/输出模型、权限、风险、超时、重试与 handler |
| `execution_context.py` | 服务端可信执行上下文：用户、租户、权限、审批结果和业务服务 |
| `search_order_tool.py` | 只读订单查询工具与内存 DemoOrderService |
| `order_schemas.py` | 查询订单的 Pydantic 输入/输出 Schema |
| `tool_messages.py` | Runtime 成功/失败结果转换为模型可消费的 `role=tool` message |

## 运行方式

### 前置条件

- Python 3.11+（本项目使用 `StrEnum` 和 `asyncio.timeout`）
- 已安装项目依赖的 Conda 环境：`ai-agent`
- `.env` 中配置 `TALAI_API_KEY`

`mini_agent_loop.py` 当前使用 TAL 的 OpenAI-compatible endpoint 和模型 `deepseek-v4-pro`。

### 执行

```bash
conda activate ai-agent
cd ~/work/py/ai-agent/week02/2-1
python -W error::RuntimeWarning mini_agent_loop.py
```

`-W error::RuntimeWarning` 会将“协程未被 await”等 RuntimeWarning 升级为错误，便于尽早发现 async 调用链问题。

## 本次改动说明

### 新增 `mini_agent_loop.py`

新增一个最小 Agent Loop：

1. 根据 `ExecutionContext.permission` 调用 `runtime.model_tools(ctx)`，只把当前用户有权使用的工具暴露给模型；
2. 调用 LLM，并将 assistant message（含 tool calls）保存进对话历史；
3. 对每个 Tool Call 构造内部 `ToolCall`，交给 `await runtime.execute(call, ctx)`；
4. 将 Runtime 返回的 `ToolResultMessage` 作为 `role=tool` message 写回历史；
5. 模型未返回 Tool Call 时结束；连续执行超过 `MAX_STEPS = 5` 时失败退出。

### 完善 `ToolRuntime.execute()`

`ToolRuntime` 现在承担工具执行的完整治理链：

```text
工具查找
→ Pydantic JSON 参数校验
→ 权限校验
→ 高风险工具审批校验
→ async timeout
→ 幂等工具的瞬时故障重试
→ Pydantic 输出校验
→ 成功/失败 ToolResultMessage
→ 结构化 Trace
```

关键点：工具 handler 是 `async def`，因此 Runtime 内部必须执行：

```python
raw_output = await tool.handler(args, context)
```

不能只在上层写 `await runtime.execute(...)`。后者只能等待 Runtime 本身，不能自动等待 Runtime 内部创建的 coroutine。

### 新增错误语义

`ToolErrorCode` 增加：

- `approval_required`：高风险工具尚未获得对应 `tool_call_id` 的人工确认；
- `invalid_output`：工具 handler 返回值不符合声明的 `output_model`。

### Trace 字段

每次工具执行完成或失败都会输出：

```json
{
  "trace_id": "...",
  "tool_call_id": "...",
  "tool_name": "search_orders",
  "user_id": "...",
  "tenant_id": "...",
  "status": "success | error",
  "attempt": 1,
  "latency_ms": 10,
  "error_code": null
}
```

## 已验证结果

已在 `ai-agent` 环境运行：

```bash
conda run -n ai-agent python -W error::RuntimeWarning mini_agent_loop.py
```

实际链路完成两轮：

```text
第 1 轮：模型调用 search_orders → Runtime 查询订单 → 写回 role=tool 结果
第 2 轮：模型读取订单结果 → 返回最终自然语言回答
```

进程以退出码 `0` 结束，未出现 `coroutine was never awaited` 警告。

## 当前边界

这是学习用的最小实现，仍有意保留的简化：

- `DemoOrderService` 是内存数据，不是实际数据库或远程订单服务；
- Trace 当前输出到控制台，尚未接入日志平台或持久化审计存储；
- 没有并发限制、租户配额、结果脱敏/截断与缓存策略；
- Prompt、模型名和 demo 身份在示例中写死，生产环境应移至受控配置与认证上下文。
