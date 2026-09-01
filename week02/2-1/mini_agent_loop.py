import asyncio
import json
import os
from dataclasses import replace

from dotenv import load_dotenv
from openai import OpenAI

from agent_run_state import CompletedRun, WaitingForApproval
from execution_context import ExecutionContext
from search_order_tool import CANCEL_ORDER, SEARCH_ORDERS, DemoOrderService
from tool_messages import ToolResultMessage
from tool_runtime import ToolCall, ToolRuntime

MAX_STEPS = 5


async def trace_writer(event: dict) -> None:
    print(f"Runtime Trace: {json.dumps(event, ensure_ascii=False)}")


def is_approval_required(result: ToolResultMessage) -> bool:
    if not result.is_error:
        return False
    return json.loads(result.content)["error"]["code"] == "approval_required"


async def _continue_order_agent(
    *,
    messages: list[dict],
    runtime: ToolRuntime,
    ctx: ExecutionContext,
    client: OpenAI,
    start_step: int,
) -> CompletedRun | WaitingForApproval:
    """从既有消息历史继续运行；遇到审批则挂起，而非把审批错误交给 LLM。"""
    for step in range(start_step, MAX_STEPS + 1):
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
            return CompletedRun(answer=assistant.content or "")

        print(f"模型返回 {len(assistant.tool_calls)} 个函数")
        for raw_call in assistant.tool_calls:
            call = ToolCall(
                id=raw_call.id,
                name=raw_call.function.name,
                arguments_json=raw_call.function.arguments,
            )
            print(
                f"执行 Runtime: id={call.id}, name={call.name}, "
                f"arguments={call.arguments_json}"
            )
            result = await runtime.execute(call, ctx)

            if is_approval_required(result):
                return WaitingForApproval(
                    pending_call=call,
                    messages=messages,
                    context=ctx,
                    next_step=step + 1,
                    approval_prompt=(
                        f"即将执行工具：{call.name}\n"
                        f"参数：{call.arguments_json}\n"
                        "是否确认？"
                    ),
                )

            messages.append(result.to_model_message())
            print(f"写回 Tool Result: {result.content}")

    raise RuntimeError("agent exceeded maximum steps")


async def run_order_agent(
    user_text: str,
    runtime: ToolRuntime,
    ctx: ExecutionContext,
    client: OpenAI,
) -> CompletedRun | WaitingForApproval:
    messages = [
        {"role": "system", "content": "你是订单助手，不得编造订单。"},
        {"role": "user", "content": user_text},
    ]
    return await _continue_order_agent(
        messages=messages,
        runtime=runtime,
        ctx=ctx,
        client=client,
        start_step=1,
    )


async def resume_order_agent(
    pending: WaitingForApproval,
    runtime: ToolRuntime,
    client: OpenAI,
) -> CompletedRun | WaitingForApproval:
    """用户确认后，以原始 ToolCall 和原始消息历史恢复，不重新请求模型生成操作。"""
    approved_ctx = replace(
        pending.context,
        approved_call_ids=pending.context.approved_call_ids | frozenset({pending.pending_call.id}),
    )
    result = await runtime.execute(pending.pending_call, approved_ctx)
    pending.messages.append(result.to_model_message())
    print(f"确认后执行 Tool Result: {result.content}")

    return await _continue_order_agent(
        messages=pending.messages,
        runtime=runtime,
        ctx=approved_ctx,
        client=client,
        start_step=pending.next_step,
    )


def reject_order_agent(_: WaitingForApproval) -> CompletedRun:
    """用户拒绝时不执行 Handler，直接结束本次运行。"""
    return CompletedRun(answer="好的，已取消本次操作，不会修改订单。")


async def main() -> None:
    runtime = ToolRuntime(trace_writer=trace_writer)
    runtime.register(SEARCH_ORDERS)
    runtime.register(CANCEL_ORDER)

    load_dotenv()
    client = OpenAI(
        base_url="http://ai-service.tal.com/openai-compatible/v1",
        api_key=os.getenv("TALAI_API_KEY"),
    )
    ctx = ExecutionContext(
        user_id="262789",
        tenant_id="test",
        permission=frozenset({"order:write", "order:read"}),
        trace_id="trace_id_123",
        order_service=DemoOrderService(),
    )

    run = await run_order_agent(
        "取消订单id为ord_1003的订单，取消原因：订单重复",
        runtime,
        ctx,
        client,
    )
    if isinstance(run, WaitingForApproval):
        print(f"\n=== 等待确认 ===\n{run.approval_prompt}")
        confirmed = input("输入 yes 确认，其他任意内容拒绝： ").strip().lower() == "yes"
        run = await resume_order_agent(run, runtime, client) if confirmed else reject_order_agent(run)

    print("\n=== 最终状态 ===")
    if isinstance(run, CompletedRun):
        print(run.answer)
    else:
        print(run.approval_prompt)


if __name__ == "__main__":
    asyncio.run(main())
