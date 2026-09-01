import json
import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from pydantic import ValidationError

from execution_context import ExecutionContext
from tool_contracts import ToolError, ToolErrorCode, ToolExecutionError
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
        """只向模型暴露当前身份有资格调用的工具。

        permission=None 表示该工具不要求业务权限；这不等于绕过后续的
        风险确认、参数校验或租户隔离。
        """
        return [
            tool.to_model_tools()
            for tool in self._tools.values()
            if tool.permission is None or tool.permission in ctx.permission
        ]

    async def _finish_error(
        self,
        *,
        tool: ToolDefinition | None,
        call: ToolCall,
        ctx: ExecutionContext,
        started: float,
        error: ToolError,
        attempt: int = 0,
    ) -> ToolResultMessage:
        """记录失败审计事件，并将统一错误协议返回给模型。"""
        await self._write_trace(
            tool=tool,
            call=call,
            ctx=ctx,
            started=started,
            status="error",
            attempt=attempt,
            error_code=error.code,
        )
        return error_message(call, error)

    async def _write_trace(
        self,
        *,
        tool: ToolDefinition | None,
        call: ToolCall,
        ctx: ExecutionContext,
        started: float,
        status: str,
        attempt: int,
        error_code: str | None = None,
    ) -> None:
        # audit_log 只控制“已找到工具后的工具审计”。未知工具仍记录失败，
        # 因为它可能意味着模型幻觉、客户端错误或一次攻击尝试。
        if tool is not None and not tool.audit_log:
            return

        await self._trace_writer({
            "trace_id": ctx.trace_id,
            "tool_call_id": call.id,
            "tool_name": call.name,
            "tool_version": tool.version if tool is not None else None,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "status": status,
            "attempt": attempt,
            "latency_ms": int((perf_counter() - started) * 1000),
            "error_code": error_code,
        })

    def _make_error(
        self,
        tool: ToolDefinition | None,
        ctx: ExecutionContext,
        *,
        code: ToolErrorCode,
        message: str,
        retryable: bool = False,
    ) -> ToolError:
        """用工具声明的错误模型创建可安全返回给模型的错误。"""
        error_model = tool.error_model if tool is not None else ToolError
        return error_model(
            code=code,
            message=message,
            retryable=retryable,
            trace_id=ctx.trace_id or None,
        )

    async def execute(self, tool_call: ToolCall, context: ExecutionContext) -> ToolResultMessage:
        started = perf_counter()

        # 1. 找工具：模型输出的 name 不可信，只能从服务端注册表获取定义。
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return await self._finish_error(
                tool=None,
                call=tool_call,
                ctx=context,
                started=started,
                error=self._make_error(
                    None, context,
                    code=ToolErrorCode.NOT_FOUND,
                    message="工具不存在或当前不可用",
                ),
            )

        # 2. 参数校验：拒绝非法类型、范围以及额外字段。
        try:
            args = tool.input_model.model_validate_json(tool_call.arguments_json)
        except ValidationError as exc:
            issues = [
                {
                    "path": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in exc.errors(include_url=False, include_input=False)
            ]
            return await self._finish_error(
                tool=tool,
                call=tool_call,
                ctx=context,
                started=started,
                error=self._make_error(
                    tool, context,
                    code=ToolErrorCode.INVALID_ARGUMENT,
                    message=json.dumps(issues, ensure_ascii=True),
                ),
            )

        # 3. 授权：None 代表“不需要特定业务 permission”，不是跳过其他治理。
        if tool.permission is not None and tool.permission not in context.permission:
            return await self._finish_error(
                tool=tool,
                call=tool_call,
                ctx=context,
                started=started,
                error=self._make_error(
                    tool, context,
                    code=ToolErrorCode.PERMISSION_DENIED,
                    message="当前用户没有该工具权限",
                ),
            )

        # risk 是影响分级；requires_confirmation 是显式确认开关。
        # 当前全局保守策略：所有 high 风险工具默认也必须确认。
        needs_confirmation = tool.requires_confirmation or tool.risk == "high"
        if needs_confirmation and tool_call.id not in context.approved_call_ids:
            return await self._finish_error(
                tool=tool,
                call=tool_call,
                ctx=context,
                started=started,
                error=self._make_error(
                    tool, context,
                    code=ToolErrorCode.APPROVAL_REQUIRED,
                    message="该工具调用需要用户确认",
                ),
            )

        # 4. 受控执行。max_retries 是“首次执行后的额外重试次数”，故总尝试数为 +1。
        for attempt in range(1, tool.max_retries + 2):
            try:
                async with asyncio.timeout(tool.timeout_seconds):
                    raw_output = await tool.handler(args, context)

                output = tool.output_model.model_validate(raw_output)
                await self._write_trace(
                    tool=tool,
                    call=tool_call,
                    ctx=context,
                    started=started,
                    status="success",
                    attempt=attempt,
                )
                return success_message(tool_call, output)
            except TimeoutError:
                error = self._make_error(
                    tool, context,
                    code=ToolErrorCode.TIMEOUT,
                    message="工具执行超时",
                    retryable=tool.idempotency,
                )
            except TransientToolError:
                error = self._make_error(
                    tool, context,
                    code=ToolErrorCode.UPSTREAM_ERROR,
                    message="上游执行错误",
                    retryable=tool.idempotency,
                )
            except ToolExecutionError as exc:
                error = self._make_error(
                    tool,
                    context,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                )
            except ValidationError:
                error = self._make_error(
                    tool, context,
                    code=ToolErrorCode.INVALID_OUTPUT,
                    message="工具调用返回结果格式错误",
                )
            except Exception:
                # 不向模型泄露内部异常、SQL、token 或堆栈；完整异常应由日志系统另存。
                error = self._make_error(
                    tool, context,
                    code=ToolErrorCode.UPSTREAM_ERROR,
                    message="工具执行失败",
                )

            should_retry = error.retryable and tool.idempotency and attempt <= tool.max_retries
            if not should_retry:
                return await self._finish_error(
                    tool=tool,
                    call=tool_call,
                    ctx=context,
                    started=started,
                    error=error,
                    attempt=attempt,
                )

            await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))

        raise AssertionError("unreachable")
