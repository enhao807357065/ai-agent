import asyncio
import json
import os
from dotenv import load_dotenv

from tool_runtime import ToolRuntime, ToolCall
from execution_context import ExecutionContext
from search_order_tool import SEARCH_ORDERS, DemoOrderService
from openai import OpenAI

MAX_STEPS = 5

async def trace_writer(event: dict) -> None:
    print(f"Runtime Trace: {json.dumps(event, ensure_ascii=False)}")

async def run_order_agent(user_text: str, runtime: ToolRuntime, ctx: ExecutionContext, client: OpenAI) -> str:
    messages = [
        {"role": "system", "content": "你是订单助手，不得编造订单。"},
        {"role": "user", "content": user_text},
    ]

    for step in range(1, MAX_STEPS+1):
        visible_tools = runtime.model_tools(ctx)
        print(f"\n=== 第 {step} 轮：计算当前可见工具 ===")
        print([tool["function"]["name"] for tool in visible_tools])
        print("=== 请求 DeepSeek ===")

        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=messages,
            tools=visible_tools,
            tool_choice="auto",
            extra_body={"thinking": {"type": "disabled"}},
        )

        assistant = response.choices[0].message
        messages.append(assistant.model_dump(exclude_none=True))
        if not assistant.tool_calls:
            print("模型未返回 Tool Call，Agent Loop 结束。")
            return assistant.content or ""

        print(f"模型返回 {len(assistant.tool_calls)} 个函数")
        for raw_call in assistant.tool_calls:
            call = ToolCall(
                id=raw_call.id,
                name=raw_call.function.name,
                arguments_json=raw_call.function.arguments
            )
            print(
                f"执行 Runtime: id={call.id}, name={call.name}, "
                f"arguments={call.arguments_json}"
            )

            result = await runtime.execute(call, ctx)
            messages.append(result.to_model_message())
            print(f"写回 Tool Result: {result.content}")

    raise RuntimeError("agent exceeded maximum steps")


async def main():
    runtime = ToolRuntime(trace_writer=trace_writer)
    runtime.register(SEARCH_ORDERS)

    load_dotenv()
    apikey = os.getenv("TALAI_API_KEY")

    client = OpenAI(
        base_url="http://ai-service.tal.com/openai-compatible/v1",
        api_key=apikey
    )

    ctx = ExecutionContext(
        user_id="262789",
        tenant_id="test",
        permission=frozenset({"order:read"}),
        trace_id="trace_id_123",
        order_service=DemoOrderService()
    )

    answer = await run_order_agent("给我查询昨天的订单", runtime, ctx, client)
    print("\n=== 最终回答 ===")
    print(answer)

if __name__ == '__main__':
    asyncio.run(main())