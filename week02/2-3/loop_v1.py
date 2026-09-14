"""最小 Agent Loop：真实/模拟 LLM → Tool Runtime → MCP Server。

默认使用 DemoProvider 验证闭环：
    conda run -n ai-agent python loop_v1.py

使用 OpenAI-compatible 真实模型：
    TALAI_API_KEY=... TALAI_BASE_URL=... LLM_MODEL=... \
    conda run -n ai-agent python loop_v1.py --llm "查询订单 ord_1002 的状态"

本文件只接入本目录的订单 MCP Server（stdio），但工具通过 tools/list 动态发现。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from dotenv import load_dotenv
from jsonschema import ValidationError, validate
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_types import CallToolResult
from openai import AsyncOpenAI

BASE_DIR = Path(__file__).parent
print(f"BASE_DIR: {BASE_DIR}")
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class ToolCall:
    """模型提出、但尚未被 Runtime 信任或执行的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    """Agent Loop 每轮获得的模型结果：最终文本或一个工具调用。"""

    text: str | None = None
    tool_call: ToolCall | None = None


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    """Host 的工具契约与 MCP 路由信息。"""

    # LLM/Host 使用的唯一名字；V1 用 server_id + remote_name 解决同名冲突。
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None

    # Host 内部路由数据：模型不能控制这些字段。
    server_id: str
    remote_name: str
    handler: ToolHandler

    def to_model_tool(self) -> dict[str, Any]:
        """导出为 OpenAI-compatible function-calling Schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class AgentProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        ...


class OpenAICompatibleProvider:
    """将 OpenAI-compatible Chat Completions Tool Call 规范化为 ModelReply。"""

    def __init__(self) -> None:
        api_key = os.getenv("TALAI_API_KEY")
        if not api_key:
            raise RuntimeError("请先设置 TALAI_API_KEY，或不传 --llm 使用 DemoProvider")

        # self.client = AsyncOpenAI(
        #     api_key=api_key,
        #     base_url=os.getenv("TALAI_BASE_URL", "https://api.deepseek.com"),
        #     max_retries=0,
        # )
        # self.model = os.getenv("LLM_MODEL", "deepseek-v4-pro")
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="http://localhost:8000/v1"
        )
        self.model = "chat-default"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        print(f"messages: {messages}")
        print(f"tools: {tools}")
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message

        # V1 明确只支持每一轮一个工具调用，避免悄悄丢弃并行调用。
        if len(message.tool_calls or []) > 1:
            raise RuntimeError("MULTIPLE_TOOL_CALLS_NOT_SUPPORTED")
        if not message.tool_calls:
            return ModelReply(text=message.content or "")

        call = message.tool_calls[0]
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("MODEL_TOOL_ARGUMENTS_INVALID") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("MODEL_TOOL_ARGUMENTS_MUST_BE_OBJECT")

        return ModelReply(
            text=message.content,
            tool_call=ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=arguments,
            ),
        )


class ToolRuntime:
    """工具执行边界：查找、输入/输出校验、超时、结构化错误和最小审计。"""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.audit_events: list[dict[str, Any]] = []

    def _audit(
        self,
        *,
        event: str,
        call: ToolCall,
        latency_ms: int | None = None,
        code: str | None = None,
    ) -> None:
        # 仅记录参数键，不记录订单号、联系人资料等潜在敏感值。
        payload: dict[str, Any] = {
            "event": event,
            "tool_call_id": call.id,
            "tool_name": call.name,
            "argument_keys": sorted(call.arguments),
        }
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        if code is not None:
            payload["code"] = code
        self.audit_events.append(payload)

    def _error(
        self,
        *,
        call: ToolCall,
        code: str,
        message: str | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        self._audit(
            event="tool_rejected" if latency_ms is None else "tool_failed",
            call=call,
            latency_ms=latency_ms,
            code=code,
        )
        result: dict[str, Any] = {"ok": False, "code": code}
        if message is not None:
            result["message"] = message
        return result

    async def execute(
        self,
        call: ToolCall,
        tools: dict[str, ToolDefinition],
    ) -> dict[str, Any]:
        tool = tools.get(call.name)
        if tool is None:
            return self._error(call=call, code="TOOL_NOT_FOUND")

        try:
            validate(instance=call.arguments, schema=tool.input_schema)
        except ValidationError as exc:
            return self._error(
                call=call,
                code="INVALID_ARGUMENT",
                message=exc.message,
            )

        started = time.monotonic()
        self._audit(event="tool_started", call=call)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                payload = await tool.handler(call.arguments)
        except TimeoutError:
            latency_ms = int((time.monotonic() - started) * 1_000)
            return self._error(
                call=call,
                code="MCP_TIMEOUT",
                latency_ms=latency_ms,
            )
        except Exception:
            latency_ms = int((time.monotonic() - started) * 1_000)
            # 内部异常的完整细节应留在服务端日志；这里不写回 LLM。
            return self._error(
                call=call,
                code="MCP_CALL_FAILED",
                latency_ms=latency_ms,
            )

        latency_ms = int((time.monotonic() - started) * 1_000)
        if not payload.get("ok"):
            self._audit(
                event="tool_failed",
                call=call,
                latency_ms=latency_ms,
                code=str(payload.get("code", "MCP_TOOL_ERROR")),
            )
            return {"tool_call_id": call.id, **payload}

        if tool.output_schema is not None:
            try:
                validate(instance=payload, schema=tool.output_schema)
            except ValidationError:
                return self._error(
                    call=call,
                    code="INVALID_MCP_OUTPUT",
                    latency_ms=latency_ms,
                )

        self._audit(event="tool_succeeded", call=call, latency_ms=latency_ms)
        return {"tool_call_id": call.id, **payload}


def normalize_mcp_result(
    result: CallToolResult,
    max_chars: int = 2_000,
) -> dict[str, Any]:
    """将 MCP SDK Result 转成大小受限的 Host payload。"""
    print(f"normalize_mcp_result result: {result.model_dump_json()}")
    print(f"structured_content: {result.structured_content}")
    print(f"content: {result.content}")
    if result.is_error:
        text = getattr(result.content[0], "text", "MCP_TOOL_ERROR")
        return {"ok": False, "code": "MCP_TOOL_ERROR", "message": text[:max_chars]}

    if result.structured_content is not None:
        value = result.structured_content
        if not isinstance(value, dict):
            return {"ok": False, "code": "INVALID_MCP_STRUCTURED_RESULT"}

        serialized = json.dumps(value, ensure_ascii=False)
        if len(serialized) > max_chars:
            return {"ok": False, "code": "RESULT_TOO_LARGE"}
        return dict(value)

    text = getattr(result.content[0], "text", "")
    if len(text) > max_chars:
        return {"ok": False, "code": "RESULT_TOO_LARGE"}
    return {"ok": True, "text": text}


async def discover_tools(
    client: Client,
    server_id: str,
) -> dict[str, ToolDefinition]:
    """通过 MCP tools/list 动态发现，并映射为 Host ToolDefinition。"""
    remote_tools = await client.list_tools()
    definitions: dict[str, ToolDefinition] = {}

    for remote in remote_tools.tools:
        public_name = f"{server_id}_{remote.name}"
        if public_name in definitions:
            raise RuntimeError(f"DUPLICATE_TOOL_NAME: {public_name}")

        async def handler(
            arguments: dict[str, Any],
            remote_name: str = remote.name,
        ) -> dict[str, Any]:
            response = await client.call_tool(remote_name, arguments)
            return normalize_mcp_result(response)

        definitions[public_name] = ToolDefinition(
            name=public_name,
            description=(remote.description or "") + f"\n来源 MCP Server：{server_id}。",
            input_schema=remote.input_schema,
            output_schema=getattr(remote, "output_schema", None),
            server_id=server_id,
            remote_name=remote.name,
            handler=handler,
        )

    return definitions


class DemoOrderProvider:
    """确定性模拟模型：动态寻找 order_get_order，再基于结果作答。"""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        if not any(message["role"] == "tool" for message in messages):
            user_input = str(messages[0]["content"])
            order_id = "ord_1002" if "1002" in user_input else "ord_1001"
            tool_name = next(
                tool["function"]["name"]
                for tool in tools
                if tool["function"]["name"].endswith("_get_order")
            )
            return ModelReply(
                tool_call=ToolCall(
                    id="demo_call_1",
                    name=tool_name,
                    arguments={"order_id": order_id},
                )
            )

        result = json.loads(messages[-1]["content"])
        if not result.get("ok"):
            return ModelReply(text=f"订单查询失败：{result['code']}")
        return ModelReply(
            text=(
                f"订单 {result['order_id']} 当前状态：{result['status']}；"
                f"金额：{result['amount_cents']} 分。"
            )
        )


async def run_agent(
    provider: AgentProvider,
    runtime: ToolRuntime,
    tools: dict[str, ToolDefinition],
    user_input: str,
    max_rounds: int = 3,
) -> str:
    """最小 Agent Loop：模型决策 → Runtime 执行 → Tool Result 回写 → 模型总结。"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_input}]
    model_tools = [tool.to_model_tool() for tool in tools.values()]
    print("MODEL TOOL NAMES:", list(tools))

    for round_no in range(1, max_rounds + 1):
        reply = await provider.complete(messages, model_tools)
        if reply.tool_call is None:
            if reply.text is None:
                raise RuntimeError("MODEL_REPLY_INVALID")
            print(f"ROUND {round_no}: final answer")
            return reply.text

        call = reply.tool_call
        print(f"ROUND {round_no}: ToolCall = {call}")
        messages.append(
            {
                "role": "assistant",
                "content": reply.text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
        )

        result = await runtime.execute(call, tools)
        print("TOOL RESULT:", result)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    raise RuntimeError("MAX_ROUNDS_EXCEEDED")


async def main() -> None:
    use_real_llm = "--llm" in sys.argv
    user_input = " ".join(argument for argument in sys.argv[1:] if argument != "--llm")
    user_input = user_input or "查询订单 ord_1001 的状态"

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(BASE_DIR / "mcp_server_v1.py")],
        cwd=BASE_DIR,
    )
    runtime = ToolRuntime()
    provider: AgentProvider = OpenAICompatibleProvider() if use_real_llm else DemoOrderProvider()

    async with Client(stdio_client(parameters)) as client:
        tools = await discover_tools(client, server_id="order")
        print("DISCOVERED MCP TOOLS:", [tool.remote_name for tool in tools.values()])
        answer = await run_agent(provider, runtime, tools, user_input)

    print("ANSWER:", answer)
    print("AUDIT EVENTS:", json.dumps(runtime.audit_events, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
