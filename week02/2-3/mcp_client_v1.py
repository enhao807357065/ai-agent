import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, AsyncIterator

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult
from contextlib import asynccontextmanager, AsyncExitStack

base_dir = Path(__file__).parent
Transport = Literal["stdio", "streamable-http"]

@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    transport: Transport
    http_url: str | None = None

@asynccontextmanager
async def connect_mcp(transport: Transport, *, http_url: str | None = None) -> AsyncIterator[Client]:
    if transport == "stdio":
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(base_dir / "mcp_server_v1.py")],
            cwd=base_dir,
        )
        async with Client(stdio_client(parameters)) as client:
            yield client
        return

    if transport == "streamable-http":
        if http_url is None:
            raise ValueError("streamable-http 必须提供 http_url")
        async with Client(streamable_http_client(url=http_url)) as client:
            yield client
        return

    raise ValueError(f"不支持的 Transport: {transport}")

@asynccontextmanager
async def connect_all_mcp(configs: list[MCPServerConfig]) -> AsyncIterator[dict[str, Client]]:
    async with AsyncExitStack() as stack:
        clients: dict[str, Client] = {}
        for config in configs:
            if config.server_id in clients:
                raise ValueError(f"重复的MCP server_id: {config.server_id}")
            client = await stack.enter_async_context(connect_mcp(config.transport, http_url=config.http_url))
            clients[config.server_id] = client
        yield clients


def result_payload(result: CallToolResult, max_chars: int = 2_000) -> dict[str, Any]:
    """将不可信的 MCP Tool Result 归一化，并限制返回内容大小。"""
    if result.is_error:
        text = getattr(result.content[0], "text", "MCP_TOOL_ERROR")
        return {"ok": False, "code": "MCP_TOOL_ERROR", "message": text[:max_chars]}

    value = result.structured_content
    if value is None:
        value = {"text": getattr(result.content[0], "text", "")}

    if len(json.dumps(value, ensure_ascii=False)) > max_chars:
        return {"ok": False, "code": "RESULT_TOO_LARGE"}

    return dict(value)

async def main() -> None:
    """以 stdio 启动本地 Server，发现工具并演示一次订单查询。"""
    # parameters = StdioServerParameters(
    #     command=sys.executable,
    #     args=[str(base_dir / "mcp_server_v1.py")],
    #     cwd=base_dir,
    # )
    #
    # async with Client(stdio_client(parameters)) as client:
    #     tools = await client.list_tools()
    #     print("可用工具:", [tool.name for tool in tools.tools])
    #
    #     for tool in tools.tools:
    #         print(f"tool.name: {tool.name}, tool.description: {tool.description}")
    #     result = await client.call_tool("get_order", {"order_id": "ord_1001"})
    #     print(result_payload(result))

    configs = [
        MCPServerConfig(
            server_id="order-stdio",
            transport="stdio",
        ),
        MCPServerConfig(
            server_id="order-http",
            transport="streamable-http",
            http_url="http://127.0.0.1:8000/mcp",
        ),
    ]

    async with connect_all_mcp(configs) as clients:
        stdio_client = clients["order-stdio"]
        http_client = clients["order-http"]

        stdio_tools = await stdio_client.list_tools()
        http_tools = await http_client.list_tools()

        print("stdio tools:", [tool.name for tool in stdio_tools.tools])
        print("http tools:", [tool.name for tool in http_tools.tools])

        stdio_result = await stdio_client.call_tool(
            "get_order",
            {"order_id": "ord_1001"},
        )

        http_result = await http_client.call_tool(
            "get_order",
            {"order_id": "ord_1002"},
        )

        print("stdio result:", result_payload(stdio_result))
        print("http result:", result_payload(http_result))

if __name__ == "__main__":
    asyncio.run(main())