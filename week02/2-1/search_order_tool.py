import asyncio
from datetime import date
from execution_context import ExecutionContext
from order_schemas import SearchOrderInput, SearchOrdersOutput, OrderSummary
from tool_definition import ToolDefinition, ToolError
from openai import OpenAI
from dotenv import load_dotenv
import os

class DemoOrderService:
    _records = [
        {
            "user_id": "user_demo",
            "order_id": "ord_1001",
            "status": "pending",
            "created_at": "2026-08-18T10:30:00+08:00",
            "amount_cents": 2999,
        },
        {
            "user_id": "262789",
            "order_id": "ord_1002",
            "status": "pending",
            "created_at": "2026-08-18T11:00:00+08:00",
            "amount_cents": 1599,
        },
    ]

    async def search(self, *, user_id: str, status: str | None, created_from: date | None, limit: int) -> SearchOrdersOutput:
        print(f"[业务查询], user_id={user_id}, status={status}, created_from={created_from}, limit={limit}")
        records = [
            record
            for record in self._records
            if record["user_id"] == user_id
               and (status is None or record["status"] == status)
               and (created_from is None or date.fromisoformat(record["created_at"][:10]) >= created_from)
        ]

        # 复制字典中的字段，但跳过 user_id。只有字段名不是 "user_id" 时，才把这个字段放到新字典里。
        # 通常是为了避免暴露不必要的内部信息或用户标识。
        return [
            {
                key: value
                for key, value in record.items()
                if key != "user_id"
            }
            for record in records[:limit]
        ]

async def search_orders(args: SearchOrderInput, ctx: "ExecutionContext") -> SearchOrdersOutput:
    rows = await ctx.order_service.search(
        user_id=ctx.user_id,
        status=args.status,
        created_from=args.created_from,
        limit=args.limit,
    )
    return SearchOrdersOutput(
        total=len(rows),
        orders=[OrderSummary(
            order_id=order["order_id"],
            status=order["status"],
            created_at=date.fromisoformat(order["created_at"][:10]),
            amount_cents=order["amount_cents"],
        ) for order in rows]
    )

SEARCH_ORDERS = ToolDefinition(
    name="search_orders",
    description=(
        "按当前用户、订单状态和创建日期查询订单。"
        "只读取订单；不能取消、退款或修改订单。"
    ),
    input_model=SearchOrderInput,
    output_model=SearchOrdersOutput,
    error_model=ToolError,
    permission="order:read",
    risk="low",
    timeout_seconds=5,
    max_retries=3,
    handler=search_orders
)

async def main():
    args = SearchOrderInput(
        status="pending",
        limit=10
    )
    ctx = ExecutionContext(
        user_id="262789",
        tenant_id="test",
        permission=frozenset({"order:read"}),
        trace_id="trace_id_123",
        order_service=DemoOrderService()
    )
    output = await SEARCH_ORDERS.handler(args, ctx)
    print("=== 4. Output Schema 验收后的结果 ===")
    validated_output = SEARCH_ORDERS.output_model.model_validate(output)
    print(validated_output.model_dump_json(indent=2))

if __name__ == '__main__':
    asyncio.run(main())

    load_dotenv()
    client = OpenAI(
        base_url=os.getenv("TALAI_BASE_URL"),
        api_key=os.getenv("TALAI_API_KEY")
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是订单助手。需要实时订单数据时调用工具；"
                "不要编造订单，修改订单前必须先获得明确目标。"
                # "你是天气助手。需要实时查询数据时调用工具；"
                # "不要编造信息。"
            ),
        },
        {
            "role": "user",
            "content": "查一下昨天上海的天气",
        },
    ]

    response = client.chat.completions.create(
        model="deepseek-v4-pro",
        messages=messages,
        tools=[SEARCH_ORDERS.to_model_tools()],
        tool_choice="auto",
        extra_body={"thinking": {"type": "disabled"}},
    )

    # 如果给一个错误的系统提示词，可能会提示【需要先说明一下：我目前可用的工具只有「订单查询」功能，并没有实时天气查询工具。】等话术
    assistant = response.choices[0].message
    print(f"assistant: {assistant.content}")

    for call in assistant.tool_calls or []:
        print(call.id)
        print(call.function.name)
        print(call.function.arguments)