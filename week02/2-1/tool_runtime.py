import json
import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from pydantic import ValidationError

from execution_context import ExecutionContext, ToolResult, ToolError
from tool_definition import ToolDefinition
from tool_messages import ToolCall, ToolResultMessage

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

    # “这个工具所要求的权限，是否包含在当前调用者拥有的权限集合中？”
    def model_tools(self, ctx: ExecutionContext) -> list[dict]:
        return [
            tool.to_model_tools()
            for tool in self._tools.values()
            if tool.permission in ctx.permission
        ]

    async def execute(self, tool_call: ToolCall, context: ExecutionContext) -> ToolResultMessage:
        # 1. 找工具
        # 2. 参数校验
        # 3. 策略校验：权限、租户、风险等级、审批
        # 4. 在受控环境下执行：timeout/retry/tracing
        # 5. 对结果做脱敏、截断、标准化
        pass
