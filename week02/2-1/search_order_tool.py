import asyncio
from datetime import date
from execution_context import ExecutionContext
from order_schemas import SearchOrderInput, SearchOrdersOutput, OrderSummary, CancelOrderInput, CancelOrderOutput
from tool_contracts import ToolError, ToolExecutionError, ToolErrorCode
from tool_definition import ToolDefinition
from openai import OpenAI
from dotenv import load_dotenv
import os
import random

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

    async def search_order_id(self, user_id: str, order_id: str) -> SearchOrdersOutput:
        print(f"[业务订单查询], user_id={user_id}, order_id={order_id}")
        records = [
            record
            for record in self._records
            if record["user_id"] == user_id and record["order_id"] == order_id
        ]

        return SearchOrdersOutput(
            total=len(records),
            orders=[
                OrderSummary(
                    order_id=record["order_id"],
                    status=record["status"],
                    created_at=date.fromisoformat(record["created_at"][:10]),
                    amount_cents=record["amount_cents"],
                )
                for record in records
            ],
        )

    async def cancel_order(self, *, user_id: str, order_id: str, reason: str = "") -> CancelOrderOutput:
        print(f"[业务取消订单], user_id={user_id}, order_id={order_id}, reason={reason}")
        records = await self.search_order_id(user_id, order_id)
        if not records.orders:
            raise ToolExecutionError(
                code=ToolErrorCode.NOT_FOUND,
                # 不应透露该订单是否真实存在但属于别的用户
                message="订单不存在或不属于当前用户",
            )

        record = records.orders[0]
        if record.status == "shipped":
            # 已发货不能取消
            raise ToolExecutionError(
                code=ToolErrorCode.ORDER_STATUS_CANNOT_CANCEL,
                # 不应透露该订单是否真实存在但属于别的用户
                message="当前订单状态不允许取消",
            )

        await asyncio.sleep(random.randint(1, 3))

        return CancelOrderOutput(
            order_id=record.order_id,
            status="canceled",
            success=True,
            msg="订单已取消",
        )

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

# handler层可以做的很薄
async def cancel_order(args: CancelOrderInput, ctx: "ExecutionContext") -> CancelOrderOutput:
    return await ctx.order_service.cancel_order(
        user_id=ctx.user_id,
        order_id=args.order_id,
        reason=args.reason,
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

CANCEL_ORDER = ToolDefinition(
    name="cancel_order",
    description=(
        "取消当前用户指定的一笔订单。"
        "工具会自行校验订单是否存在、是否属于当前用户，以及订单当前状态是否允许取消。"
        "订单不存在、不属于当前用户或状态不允许取消时，工具会返回明确错误。"
        "该操作会真实修改订单状态，执行前需要用户确认。"
    ),
    when_to_use=(
        "用户明确要求取消订单，并提供了具体订单 ID 和取消原因时使用。"
        "用户已提供订单 ID 后，可以直接调用本工具，无需先通过订单列表查询验证该订单。"
    ),
    when_not_to_use=(
        "不要用于退款、修改收货地址、查询订单列表或取消其他用户订单。"
        "用户未提供订单 ID 或取消原因时，不要调用；应先向用户追问。"
        "不要将本工具用于仅查询订单是否存在或订单状态的场景。"
    ),
    input_model=CancelOrderInput,
    output_model=CancelOrderOutput,
    handler=cancel_order,
    permission="order:write",
    risk="high",
    requires_confirmation=True,
    idempotency=False,
    timeout_seconds=5,
    max_retries=0,
    audit_log=True
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

    # messages = [
    #     {
    #         "role": "system",
    #         "content": (
    #             "你是订单助手。需要实时订单数据时调用工具；"
    #             "不要编造订单，修改订单前必须先获得明确目标。"
    #             # "你是天气助手。需要实时查询数据时调用工具；"
    #             # "不要编造信息。"
    #         ),
    #     },
    #     {
    #         "role": "user",
    #         "content": "查一下昨天上海的天气",
    #     },
    # ]
    #
    # response = client.chat.completions.create(
    #     model="deepseek-v4-pro",
    #     messages=messages,
    #     tools=[SEARCH_ORDERS.to_model_tools(), CANCEL_ORDER.to_model_tools()],
    #     tool_choice="auto",
    #     extra_body={"thinking": {"type": "disabled"}},
    # )
    #
    # # 如果给一个错误的系统提示词，可能会提示【需要先说明一下：我目前可用的工具只有「订单查询」功能，并没有实时天气查询工具。】等话术
    # assistant = response.choices[0].message
    # print(f"assistant: {assistant.content}")
    #
    # for call in assistant.tool_calls or []:
    #     print(call.id)
    #     print(call.function.name)
    #     print(call.function.arguments)