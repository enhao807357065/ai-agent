# MCP Host（整个 Agent 应用）
# │
# ├─ User/API/UI 层
# ├─ 会话与 Agent Orchestrator
# │    └─ 驱动 LLM Agent Loop
# │
# ├─ Tool Runtime                    ← 你之前学习的部分
# │    ├─ Schema 校验
# │    ├─ 权限 / 租户隔离
# │    ├─ 审批
# │    ├─ 超时 / 重试 / 审计
# │    └─ 路由 handler
# │         ├─ Local Handler
# │         └─ MCP Handler → MCP Client → MCP Server
# │
# └─ Context 管理
#      ├─ 消息历史
#      ├─ MCP Resources
#      ├─ MCP Prompts
#      └─ Tool Results

# Host：统筹整个 Agent + MCP 生态
# Tool Runtime：把一个 Tool Call 安全地执行掉

from __future__ import annotations

import asyncio
import json
import os
import sys
from jsonschema import validate, ValidationError

from typing import Any, Protocol, Callable, Awaitable
from dataclasses import dataclass
from mcp import Client
from openai import AsyncOpenAI
from dotenv import load_dotenv

from mcp_client_v1 import MCPServerConfig, connect_all_mcp, result_payload

load_dotenv()

@dataclass(frozen=True)
class ToolCall:
    """Host 交给 Runtime 执行的一次已解析工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class ModelReply:
    """Provider 单轮返回的最终文本或单次工具调用。"""

    text: str | None = None
    tool_call: ToolCall | None = None


class LLMProvider(Protocol):
    """定义 Agent Loop 所需的模型调用抽象。"""
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        ...

class DeepseekProvider:
    """通过 OpenAI-compatible API 调用 DeepSeek，并规范化模型回复。"""

    def __init__(self):
        """读取模型配置并创建禁用 SDK 自动重试的异步客户端。"""
        api_key = os.getenv("TALAI_API_KEY")
        if not api_key:
            raise RuntimeError("请先设置环境变量 TALAI_API_KEY")
        self.model = os.getenv("LLM_MODEL", "deepseek-v4-pro")
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("TALAI_BASE_URL", "https://api.deepseek.com"),
            max_retries=0,
        )

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        """请求模型，提取首个 Tool Call 或最终文本。"""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message
        if message.tool_calls:
            call = message.tool_calls[0]
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError("MODEL_TOOL_ARGUMENTS_INVALID") from exc
            return ModelReply(
                text=message.content,
                tool_call=ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                )
            )
        return ModelReply(text=message.content or "")

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

@dataclass(frozen=True)
class ToolDefinition:
    """Host 侧工具定义，包含模型可见 Schema 与远端调用处理器。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_model_tool(self) -> dict[str, Any]:
        """转换为 OpenAI-compatible 的 function tool 描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

class ToolRuntime:
    """在调用 MCP Server 前执行工具查找、参数校验与执行轨迹记录。"""

    def __init__(self):
        """初始化空的执行轨迹，供副作用和调用顺序断言使用。"""
        self.trace: list[dict[str, Any]] = []

    def _reject(
        self,
        call: ToolCall,
        code: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """记录本地拒绝，并返回未触发 Server 调用的结构化错误。"""
        payload = {"ok": False, "code": code}
        if message:
            payload["message"] = message
        self.trace.append(
            {
                "event": "runtime_rejected",
                "tool_call_id": call.id,
                "name": call.name,
                "payload": payload,
            }
        )
        return payload

    async def execute(
        self,
        call: ToolCall,
        tools: dict[str, ToolDefinition],
    ) -> dict[str, Any]:
        tool = tools.get(call.name)
        if not tool:
            return self._reject(call, "TOOL_NOT_FOUND")

        try:
            validate(instance=call.arguments, schema=tool.input_schema)
        except ValidationError as exc:
            return self._reject(call, "INVALID_ARGUMENT", exc.message)

        self.trace.append(
            {
                "event": "server_call",
                "tool_call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )

        result = await tool.handler(call.id, call.arguments)
        self.trace.append(
            {
                "event": "tool_result",
                "tool_call_id": call.id,
                "name": call.name,
                "payload": result,
            }
        )
        return result

async def discover_tools(client: Client, server_id: str) -> dict[str, ToolDefinition]:
    """发现远端 MCP 工具，命名转换后注册为 Host 可执行工具。"""
    result = await client.list_tools()
    definitions: dict[str, ToolDefinition] = {}
    for remote in result.tools:
        qualified_name = f"{server_id}_{remote.name}"
        print(f"remote: {remote.name}, description: {remote.description}")

        async def handler(
            tool_call_id: str,
            arguments: dict[str, Any],
            remote_name: str = remote.name,
        ) -> dict[str, Any]:
            """调用原始 MCP 工具名，并将远端结果归一化为 Host Payload。"""
            response = await client.call_tool(remote_name, arguments)
            return {"tool_call_id": tool_call_id, **result_payload(response)}

        definitions[qualified_name] = ToolDefinition(
            name=qualified_name,
            description=remote.description or "",
            input_schema=remote.input_schema,
            handler=handler,
        )

    return definitions

async def run_agent(
    provider: LLMProvider,
    runtime: ToolRuntime,
    tools: dict[str, ToolDefinition],
    user_input: str,
    max_rounds: int = 3,
) -> str:
    """持续执行模型决策、Runtime 调用和结果回写，直到模型返回文本。"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_input}]
    model_tools = [tool.to_model_tool() for tool in tools.values()]

    for round_no in range(1, max_rounds+1):
        reply = await provider.complete(messages, model_tools)
        if reply.tool_call is None:
            if reply.text is None:
                raise RuntimeError("MODEL_REPLY_INVALID")
            print("LOOP", round_no, "final")
            return reply.text

        print("LOOP", round_no, "tool_call", reply.tool_call.name)
        messages.append(
            {
                "role": "assistant",
                "content": reply.text,
                "tool_calls": [
                    {
                        "id": reply.tool_call.id,
                        "type": "function",
                        "function": {
                            "name": reply.tool_call.name,
                            "arguments": json.dumps(
                                reply.tool_call.arguments,
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        )
        tool_result = await runtime.execute(reply.tool_call, tools)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": reply.tool_call.id,
                "content": json.dumps(tool_result, ensure_ascii=False),
            }
        )
        runtime.trace.append(
            {
                "event": "result_written_to_context",
                "round": round_no,
                "tool_call_id": reply.tool_call.id,
            }
        )
    raise RuntimeError("MAX_ROUNDS_EXCEEDED")


class DemoProvider:
    """不请求真实 LLM，用于验证 Host → Runtime → MCP → Host 的完整链路。"""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        if not any(message["role"] == "tool" for message in messages):
            tool_name = next(
                tool["function"]["name"]
                for tool in tools
                if tool["function"]["name"].endswith("_get_order")
            )
            return ModelReply(
                tool_call=ToolCall(
                    id="demo_call_1",
                    name=tool_name,
                    arguments={"order_id": "ord_1001"},
                )
            )

        tool_payload = json.loads(messages[-1]["content"])
        return ModelReply(
            text=(
                f"订单 {tool_payload['order_id']} 的状态是 "
                f"{tool_payload['status']}，金额为 "
                f"{tool_payload['amount_cents']} 分。"
            )
        )


async def main() -> None:
    """组装 MCP 连接、工具发现、Tool Runtime 与 Agent Loop。"""
    use_demo_provider = "--demo" in sys.argv
    user_input = next(
        (argument for argument in sys.argv[1:] if argument != "--demo"),
        "查询订单 ord_1001 的状态",
    )

    # V1 先接一个 stdio Server；增加 HTTP Server 时只需在这里追加配置。
    configs = [
        MCPServerConfig(
            server_id="order",
            transport="stdio",
        )
    ]

    runtime = ToolRuntime()
    provider: LLMProvider = DemoProvider() if use_demo_provider else DeepseekProvider()

    async with connect_all_mcp(configs) as clients:
        tools: dict[str, ToolDefinition] = {}
        for server_id, client in clients.items():
            discovered = await discover_tools(client, server_id)
            duplicate_names = tools.keys() & discovered.keys()
            if duplicate_names:
                raise RuntimeError(f"DUPLICATE_HOST_TOOL_NAMES: {sorted(duplicate_names)}")
            tools.update(discovered)

        print("HOST TOOLS:", list(tools))
        answer = await run_agent(provider, runtime, tools, user_input)

    print("ANSWER:", answer)
    print("TRACE:", json.dumps(runtime.trace, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())