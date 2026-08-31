import json
import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from pydantic import ValidationError

from execution_context import ExecutionContext, ToolResult, ToolError, ToolErrorCode
from tool_definition import ToolDefinition
from tool_messages import ToolCall, ToolResultMessage, success_message, error_message

# TraceWriter 代表“任何接收一个 dict 参数、并且需要 await、最终不返回值的可调用对象”。
# Callable[[dict], Awaitable[None]]
#          │       │
#          │       └─ 调用后得到一个可等待对象；await 完成后返回 None
#          │
#          └─ 该可调用对象接收一个 dict 参数
# 通用语法：
# # 无参数，返回字符串
# Callable[[], str]
#
# # 接收 str，返回 bool
# Callable[[str], bool]
#
# # 接收 str 和 int，返回 dict
# Callable[[str, int], dict]
#
# # 接收任意签名，返回 None
# Callable[..., None]
TraceWriter = Callable[[dict], Awaitable[None]]

class TransientToolError(Exception):
    """上游临时故障，只有幂等工具才允许重试。"""


class ToolRuntime:

    def __init__(self, trace_writer: TraceWriter):
        self._trace_writer = trace_writer
        self._tools: dict[str, ToolDefinition] = {}

    # register() 返回一个无参数、无返回值的函数。
    # 执行过程：runtime = ToolRuntime(trace_writer=write_trace)
    # dispose_search_orders = runtime.register(SEARCH_ORDERS)，
    # 而 dispose_search_orders 是内部定义的闭包函数：dispose()
    # 之后调用dispose_search_orders() 等价于 runtime._tools.pop("search_orders", None)
    # 创建方返回“撤销/释放”的能力。
    # 哪些场景会用到：1.测试隔离，2.临时工具 用完即清理，3.插件热加载/动态启停
    def register(self, tool: ToolDefinition) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ValueError(f"工具已经注册: {tool.name}")
        self._tools[tool.name] = tool

        def dispose() -> None:
            self._tools.pop(tool.name, None)

        return dispose

    # 这个工具所要求的权限，是否包含在当前调用者拥有的权限集合中？
    def model_tools(self, ctx: ExecutionContext) -> list[dict]:
        return [
            tool.to_model_tools()
            for tool in self._tools.values()
            if tool.permission in ctx.permission
        ]

    async def _finish_error(self, call: ToolCall, ctx: ExecutionContext, started: float, error: ToolError, attempt: int = 0) -> ToolResultMessage:
        await self._write_trace(
            call=call,
            ctx=ctx,
            started=started,
            status="error",
            attempt=attempt,
            error_code=error.code,
        )
        return error_message(call, error)

    async def _write_trace(self, *, call: ToolCall, ctx: ExecutionContext, started: float, status: str, attempt: int, error_code: str | None = None) -> None:
        await self._trace_writer({
            "trace_id": ctx.trace_id,
            "tool_call_id": call.id,
            "tool_name": call.name,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "status": status,
            "attempt": attempt,
            "latency_ms": int((perf_counter() - started) * 1000),
            "error_code": error_code,
        })

    async def execute(self, tool_call: ToolCall, context: ExecutionContext) -> ToolResultMessage:
        started = perf_counter()
        # 1. 找工具
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return await self._finish_error(
                call=tool_call,
                ctx=context,
                started=started,
                error=ToolError(
                    code=ToolErrorCode.NOT_FOUND,
                    message="工具不存在或当前不可用",
                )
            )
        # 2. 参数校验
        try:
            args = tool.input_model.model_validate_json(tool_call.arguments_json)
        except ValidationError as exc:
            issues = [
                {
                    "path": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in exc.errors(
                    include_url=False,
                    include_input=False,
                )
            ]
            return await self._finish_error(
                call=tool_call,
                ctx=context,
                started=started,
                error=ToolError(
                    code=ToolErrorCode.INVALID_ARGUMENT,
                    message=json.dumps(issues, ensure_ascii=True)
                )
            )
        # 3. 策略校验：权限、租户、风险等级、审批
        if tool.permission not in context.permission:
            return await self._finish_error(
                call=tool_call,
                ctx=context,
                started=started,
                error=ToolError(
                    code=ToolErrorCode.PERMISSION_DENIED,
                    message="当前用户没有该工具权限"
                )
            )

        if tool.risk == "high" and tool_call.id not in context.approved_call_ids:
            # 如果当前工具风险等级是高，并且之前没有授权过
            return await self._finish_error(
                call=tool_call,
                ctx=context,
                started=started,
                error=ToolError(
                    code=ToolErrorCode.APPROVAL_REQUIRED,
                    message="该工具需要向用户确认"
                )
            )
        # 4. 在受控环境下执行：timeout/retry/tracing
        for attempt in range(1, tool.max_retries+1):
            try:
                async with asyncio.timeout(tool.timeout_seconds):
                    raw_output = await tool.handler(args, context)

                output = tool.output_model.model_validate(raw_output)
                await self._write_trace(
                    call=tool_call,
                    ctx=context,
                    started=started,
                    status="success",
                    attempt=attempt,
                )
                return success_message(tool_call, output)
            except TimeoutError as e:
                error = ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    message="工具执行超时",
                    retryable=tool.idempotency
                )
            except TransientToolError as e:
                error = ToolError(
                    code=ToolErrorCode.UPSTREAM_ERROR,
                    message="上游执行错误",
                    retryable=tool.idempotency
                )
            except ValidationError as e:
                error = ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    message="工具调用返回结果格式错误",
                )
            except Exception:
                error = ToolError(
                    code=ToolErrorCode.UPSTREAM_ERROR,
                    message="工具执行失败",
                )

            if not (error.retryable and tool.idempotency and attempt <= tool.max_retries):
                # 能重试，并且在重试次数内
                return await self._finish_error(
                    call=tool_call,
                    ctx=context,
                    started=started,
                    error=error,
                    attempt=attempt,
                )

            await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))

        # 5. 对结果做脱敏、截断、标准化
        raise AssertionError("unreachable")
