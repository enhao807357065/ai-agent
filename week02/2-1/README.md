# 2-1：Mini Agent Loop + Tool Runtime

这个练习实现了一个可运行的最小 Agent：LLM 负责决定“是否调用什么工具”，而 `ToolRuntime` 负责决定“该调用是否能安全、可控地执行”。

它刻意把 **模型决策** 与 **服务端执行治理** 分开：模型输出的 Tool Call 只是*不可信输入*，不能直接调用 Python 函数、数据库或第三方 API。

## 学习目标

完成这一节后，应能解释并亲手实现：

- Pydantic Model 如何成为 Tool 输入、输出与错误的唯一事实来源；
- 为什么 Tool Call 的参数、工具名和工具输出都不能直接信任；
- 为什么“向模型展示工具”和“真正执行工具”都必须做权限校验；
- 写操作为什么要先挂起 Agent、等待用户确认，再以**同一个 `tool_call_id`**恢复；
- timeout、幂等重试、错误归一化、审计 Trace 在 Runtime 的位置与职责。

## 总体架构

```text
                         ┌───────────────────────────────────────┐
                         │            ExecutionContext           │
                         │ user_id / tenant_id / permissions     │
                         │ approved_call_ids / trace_id / service│
                         └──────────────────┬────────────────────┘
                                            │ 服务端可信注入
用户问题 ──> Mini Agent Loop ──> LLM ──> Tool Call（不可信）
                 │                          │
                 │                          ▼
                 │              ┌───────────────────────┐
                 │              │      ToolRuntime      │
                 │              │ 1. 从 registry 找工具 │
                 │              │ 2. Pydantic 校验参数  │
                 │              │ 3. 权限 / 审批校验    │
                 │              │ 4. timeout / retry    │
                 │              │ 5. Pydantic 校验输出  │
                 │              │ 6. Trace / 审计       │
                 │              └──────────┬────────────┘
                 │                         │
                 │             ┌───────────┴────────────┐
                 │             ▼                        ▼
                 │     普通结果：role=tool       需要确认：挂起 Run
                 │             │                        │
                 │             ▼                        ▼
                 └─────── 写回 messages         用户确认 / 拒绝
                               │                      │
                               ▼                      ▼
                        LLM 继续推理       同一 pending ToolCall 恢复
```

> **核心边界：**LLM 只能提出调用建议；`ToolRuntime` 才是实际能力的唯一入口。

## 执行流程

### 1. 普通只读 Tool：自动执行

以 `search_orders` 为例：

```text
用户：查询我的 pending 订单
  ↓
LLM：生成 search_orders 的 tool_call
  ↓
Runtime：查找工具 → 校验 JSON 参数 → 校验 order:read 权限
  ↓
Handler：只使用 ctx.user_id 查询当前用户订单
  ↓
Runtime：校验 SearchOrdersOutput → 写 Trace
  ↓
ToolResultMessage(role="tool") 写回 messages
  ↓
LLM：根据工具结果生成自然语言回答
```

### 2. 高风险写 Tool：挂起、确认、恢复

以 `cancel_order` 为例：

```text
用户：取消 ord_1002，原因是重复下单
  ↓
LLM：生成 cancel_order 的 tool_call（包含 call-original）
  ↓
Runtime：参数、权限通过；发现 requires_confirmation / risk=high
  ↓
返回 approval_required；Agent 不把它写给 LLM，而是返回 WaitingForApproval
  ↓
UI 展示 approval_prompt，用户选择确认或拒绝
  ├─ 拒绝：reject_order_agent() 直接完成，Handler 不执行
  └─ 确认：将 call-original 加入 approved_call_ids
            ↓
          用原始 pending_call 和原始 messages 恢复
            ↓
          Runtime 执行 Handler，Tool Result 写回历史
            ↓
          LLM 基于执行结果生成最终回答
```

这里的关键不是“再次请求模型生成一次取消订单调用”，而是：

```python
result = await runtime.execute(pending.pending_call, approved_ctx)
```

确认只授权 **这个已展示、已确认的 Tool Call**。如果再次让模型生成操作，参数或目标订单可能发生变化，用户看到和系统执行的就不再是同一件事。

## 目录与职责

| 文件 | 职责 |
|---|---|
| `mini_agent_loop.py` | Agent 主循环；将工具结果写回 messages；发现审批需求时挂起，确认后恢复 |
| `agent_run_state.py` | `CompletedRun` 与 `WaitingForApproval` 两种运行状态契约 |
| `tool_runtime.py` | Tool 注册、模型可见工具筛选、参数/权限/审批/超时/重试/输出校验、Trace |
| `tool_definition.py` | 不可变 Tool 元数据：输入输出模型、handler、权限、风险、确认、超时和重试 |
| `tool_contracts.py` | 跨模块共享契约：`ToolInput`、`ToolOutput`、`ToolError`、错误码和 `ToolExecutionError` |
| `tool_messages.py` | LLM Tool Call 的内部表示；将成功或安全错误转换为 `role="tool"` 消息 |
| `execution_context.py` | 服务端可信创建的身份、租户、权限、审批结果、Trace 与业务服务上下文 |
| `order_schemas.py` | 订单查询/取消订单的 Pydantic 输入输出 Schema |
| `search_order_tool.py` | `search_orders`（只读）与 `cancel_order`（高风险写）的 Handler 和内存 Demo 服务 |
| `test_tool_runtime.py` | Runtime 对公开工具、确认绑定、错误模型和审计开关的离线测试 |
| `test_approval_flow.py` | 审批恢复与拒绝路径的离线测试 |

## 关键数据契约

### ToolDefinition：能力声明，不是直接暴露给模型的函数

`ToolDefinition` 同时声明 Tool 的 LLM 契约和服务端治理元数据：

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler

    when_to_use: str = ""
    when_not_to_use: str = ""
    requires_confirmation: bool = False
    idempotency: bool = True
    permission: Permission | None = None
    risk: RiskLevel = "low"
    timeout_seconds: int = 10
    max_retries: int = 0
    error_model: type[ToolError] = ToolError
    audit_log: bool = True
```

`input_model.model_json_schema()` 会投影为 Function Calling 的 `parameters`；但 JSON Schema 只用于告诉模型“应该怎样传”。真正执行前，Runtime 仍必须使用：

```python
args = tool.input_model.model_validate_json(tool_call.arguments_json)
```

校验来自 LLM 的原始 JSON，并拒绝未知字段、类型错误、缺失字段和业务范围外的值。

### ExecutionContext：身份必须由服务端注入

```python
@dataclass(frozen=True)
class ExecutionContext:
    user_id: str
    tenant_id: str
    permission: frozenset[str]
    approved_call_ids: frozenset[str]
    trace_id: str
    order_service: object | None
```

不要让 LLM 参数决定 `user_id`、`tenant_id`、权限或审批状态。比如 `search_orders` 的 Handler 永远从 `ctx.user_id` 读取用户身份：

```python
rows = await ctx.order_service.search(
    user_id=ctx.user_id,
    status=args.status,
    created_from=args.created_from,
    limit=args.limit,
)
```

这避免了模型传入别人的用户 ID 后越权读取订单。

### ToolError：错误也是契约

Runtime 不应把 Python 堆栈、SQL、token、下游异常文本原样塞进 LLM 上下文。它将错误标准化为：

```json
{
  "error": {
    "code": "permission_denied",
    "message": "当前用户没有该工具权限",
    "retryable": false,
    "trace_id": "trace-1"
  }
}
```

常见错误分层：

| 类别 | 代表错误码 | 是否通常可重试 |
|---|---|---|
| 参数/调用错误 | `invalid_argument` | 否 |
| 鉴权/审批 | `permission_denied`、`approval_required` | 否，需要新权限或用户动作 |
| 业务状态 | `not_found`、`can_not_cancel` | 否 |
| 系统暂态问题 | `timeout`、`upstream_error` | 取决于 Tool 是否幂等 |
| Runtime 契约问题 | `invalid_output` | 否，应修复 Handler 或输出 Schema |

Handler 如果有明确且可安全暴露的业务错误，应抛出 `ToolExecutionError`；Runtime 再统一转换为 `ToolError`。未知异常则只返回通用 `upstream_error`，避免泄露内部细节。

## ToolRuntime 的治理顺序

`ToolRuntime.execute()` 的顺序是安全边界，而非实现细节：

```text
1. registry 按 tool name 找 ToolDefinition
2. Pydantic 解析、校验 LLM arguments_json
3. 执行层再次校验权限
4. 校验该 call 是否需要、且已经得到确认
5. 在 timeout 内 await Handler
6. 仅对幂等 Tool 的可重试异常进行退避重试
7. Pydantic 校验 Handler 输出
8. 转为 ToolResultMessage，并记录 Trace
```

### 1. 展示层过滤 ≠ 执行层鉴权

`model_tools(ctx)` 会先隐藏无权限工具，减少 token 和误调用：

```python
if tool.permission is None or tool.permission in ctx.permission
```

但这只是展示层优化。模型仍可能幻觉出一个隐藏工具名或通过其他输入路径抵达 Runtime，所以 `execute()` 必须再次检查相同权限。

### 2. 为什么内部必须 `await handler`

`handler` 的类型是异步函数：

```python
Handler = Callable[[BaseModel, ExecutionContext], Awaitable[BaseModel]]
```

因此 Runtime 内部必须等待它：

```python
raw_output = await tool.handler(args, context)
```

只在外层写 `await runtime.execute(...)` 并不能自动等待 Runtime 内部创建的 coroutine；若漏掉内部 `await`，就会出现：

```text
RuntimeWarning: coroutine '...' was never awaited
```

### 3. timeout 与重试

Runtime 用 `asyncio.timeout()` 限制单次尝试：

```python
async with asyncio.timeout(tool.timeout_seconds):
    raw_output = await tool.handler(args, context)
```

`max_retries` 表示**首次执行后的额外重试次数**，所以总尝试次数为：

```text
max_retries + 1
```

只有同时满足以下条件才会重试：

```python
error.retryable and tool.idempotency and attempt <= tool.max_retries
```

这就是为什么 `search_orders` 可配置重试，而 `cancel_order` 必须配置 `idempotency=False`、`max_retries=0`：网络超时不代表取消操作没有成功，盲目重试可能造成重复写入。

### 4. 输出也必须校验

Handler 返回的可能是内部 DTO、下游 API 响应或被污染的第三方数据。Runtime 必须在写回 LLM 前验收：

```python
output = tool.output_model.model_validate(raw_output)
```

这样未声明字段、错误类型或不符合对外契约的返回都会被拦截为 `invalid_output`，而不是直接进入模型上下文。

## 订单 Tool 示例

| Tool | 权限 | 风险 | 确认 | 幂等 | 作用 |
|---|---|---|---|---|---|
| `search_orders` | `order:read` | `low` | 否 | 是 | 查询当前用户订单 |
| `cancel_order` | `order:write` | `high` | 是 | 否 | 取消当前用户的一笔订单 |

`cancel_order` 的业务层还会检查：

- 订单是否存在且属于当前用户；
- 当前订单状态是否允许取消；
- 取消原因；
- 最终状态与响应 Schema 是否匹配。

因此 `order:write` 只是一个 **capability boundary（能力边界）**，不等于“获得该权限后就可修改任意订单”。具体资源归属、业务状态、金额边界和审批仍要在执行路径验证。

## 运行方式

### 前置条件

- Python 3.11+（使用 `StrEnum` 和 `asyncio.timeout`）；
- 已安装项目依赖的 Conda 环境：`ai-agent`；
- 如需运行真实 LLM Demo，`.env` 中已配置 `TALAI_API_KEY`；
- `mini_agent_loop.py` 使用 TAL 的 OpenAI-compatible endpoint 与 `deepseek-v4-pro`。

### 先运行离线测试（推荐）

测试不依赖真实 LLM、网络或 API Key：

```bash
cd ~/work/py/ai-agent/week02/2-1
conda run -n ai-agent python -m unittest \
  test_tool_runtime.py \
  test_approval_flow.py \
  -v
```

重点验证：

- 无权限限制的 Tool 可以展示并执行；
- `approval_required` 只会被 `approved_call_ids` 中的同一 `tool_call_id` 放行；
- 用户拒绝不会执行 Handler；
- 用户确认后使用原始 Tool Call 恢复并写回 tool message；
- 自定义错误模型、审计开关与 Trace 字段生效。

### 运行真实交互 Demo

```bash
conda activate ai-agent
cd ~/work/py/ai-agent/week02/2-1
python -W error::RuntimeWarning mini_agent_loop.py
```

Demo 会要求模型处理取消订单请求。收到确认提示后：

- 输入 `yes`：使用原始 `pending_call` 执行取消，再让模型生成最终回答；
- 输入其他任意内容：直接结束，订单不会被修改。

`-W error::RuntimeWarning` 会将协程未等待等 RuntimeWarning 升级为错误，能尽早暴露 async 调用链问题。

## Trace 与可观测性

每次实际执行成功或失败，Runtime 会调用注入的 `trace_writer`。当前 Demo 输出到控制台：

```json
{
  "trace_id": "trace-approval",
  "tool_call_id": "call-original",
  "tool_name": "cancel_order",
  "tool_version": "v1",
  "user_id": "262789",
  "tenant_id": "test",
  "status": "success",
  "attempt": 1,
  "latency_ms": 1000,
  "error_code": null
}
```

`audit_log=False` 可以关闭**已找到 Tool**的常规审计；但未知工具仍会被记录，因为它可能代表模型幻觉、客户端错误或攻击尝试。

生产环境应将 Trace 发送到结构化日志、指标与审计存储，而不是仅 `print()`；同时要避免记录完整敏感参数或完整工具输出。

## 当前边界与下一步

这是用于理解 Runtime 边界的最小教学实现，以下能力仍未实现或只保留了字段：

- `DemoOrderService` 是内存数据，且取消操作只是返回 `canceled`，没有持久化真实状态；
- `result_cache_policy`、`concurrency_limit`、`retry_policy` 还是声明字段，尚未被 Runtime 落地；
- 没有全局/租户级限流、预算、队列、公平调度和真正的熔断器；
- 输出尚未实现大小截断、敏感字段脱敏、HTML/二进制清理和总 token budget；
- `WaitingForApproval` 仅存在内存；生产环境要持久化 Run、消息历史、审批记录和幂等键；
- 当前是 CLI 的 `input()` 确认；真实服务应由前端/API 返回明确的 `waiting_for_approval` 状态，并由受审计的确认接口恢复；
- Tool 的权限目前是单个字符串；复杂场景可演进为 scope 集合、资源级授权与策略引擎。

> 最终要记住：**Tool 不是“给 LLM 调用的函数”，而是带有输入输出契约、权限边界、风险确认、可靠性策略与审计能力的受控能力单元。**
