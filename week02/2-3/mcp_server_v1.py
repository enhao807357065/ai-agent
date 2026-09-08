from __future__ import annotations

from typing import Any
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import sys

# 方式1:动态发现 MCP Tools。优点：接入新 MCP Server 后，工具可自动发现。
# 风险：不能把 Server 返回的全部工具直接暴露给模型；仍须经过权限、风险、租户策略过滤。
# Agent Run 初始化
#   ↓
# MCP Client → MCP Server: tools/list
#   ↓
# 拿到 MCP Tool Schema
#   ↓
# 映射 / 过滤为可用内部工具
#   ↓
# 传给 LLM
#   ↓
# LLM 选工具后 → MCP tools/call

# 方式2:静态注册 + MCP Handler。优点：既有 Agent Loop 和 Tool Runtime 基本不用变；安全控制仍在自己手中。
# 代价：MCP Server 新增/变更工具时，需要更新本地 Registry 或映射配置。
# 内部 ToolDefinition（稳定公开契约）
#   ↓
# Tool Runtime：校验、鉴权、审批、审计
#   ↓
# MCPHandler
#   ↓
# MCP Client → MCP Server: tools/call

# MCP中，server可以向agent暴露3类能力
# Tool      = 让 Agent 去“做事”
# Resource  = 给 Agent / 用户“读取资料”
# Prompt    = 给 Agent “套用工作模板”

mcp = MCPServer(
    name="order-service",
    instructions="查询订单必须先调用 get_order，不要猜测订单状态。"
)

ORDERS = {
    "ord_1001": {
        "order_id": "ord_1001",
        "status": "pending",
        "amount_cents": 2999,
    },
    "ord_1002": {
        "order_id": "ord_1002",
        "status": "shipped",
        "amount_cents": 1599,
    },
}

# 可执行的动作， Tool 类似 Function Calling 里的函数。
@mcp.tool(structured_output=True)
def get_order(order_id: str) -> dict[str, Any]:
    """按订单号查询订单状态。找不到订单时返回工具错误。"""
    order = ORDERS.get(order_id)
    if not order:
        raise ToolError("ORDER_NOT_FOUND")
    return {"ok": True, **order}

@mcp.tool(structured_output=True)
def create_ticket(order_id: str, reason: str) -> dict[str, object]:
    """为已存在的订单创建售后工单。"""
    if order_id not in ORDERS:
        raise ToolError("ORDER_NOT_FOUND")
    if not 4 <= len(reason) <= 100:
        raise ToolError("INVALID_REASON")
    return {"ok": True, "ticket_id": "ticket_2001", "order_id": order_id}

# Resource：可读取的上下文资料
@mcp.resource(uri="policy://order-status")
def order_status_policy() -> str:
    """订单状态说明，只提供只读上下文。"""
    return "pending=待处理；shipped=已发货；cancelled=已取消"

# Prompt：服务器提供的任务模板
@mcp.prompt()
def order_assistant(order_id: str) -> str:
    """生成订单查询任务模板。"""
    return f"查询订单 {order_id}。必须使用工具获取真实状态，不要猜测。"

if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run("streamable-http", host="127.0.0.1", port=8000)
    else:
        mcp.run("stdio")
