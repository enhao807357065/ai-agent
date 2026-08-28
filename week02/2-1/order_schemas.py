from pydantic import Field, model_validator
from typing import Literal
from datetime import date
from execution_context import ToolInput, ToolOutput

# class StrictModel(BaseModel):
#     model_config = ConfigDict(extra="forbid", strict=True)


# 验收模型的参数是否符合要求
class SearchOrderInput(ToolInput):
    status: Literal["pending", "paid", "shipped", "canceled"] | None = None
    created_from: date | None = None
    limit: int = Field(default=10, ge=1, le=20, description="需要查询的数量")

    @model_validator(mode="after")
    def require_check(self) -> "SearchOrderInput":
        if self.status is None and self.created_from is None:
            raise ValueError("status 与 created_from 至少提供一个")
        return self

class OrderSummary(ToolOutput):
    order_id: str
    status: Literal["pending", "paid", "shipped", "canceled"] | None = None
    created_at: date | None = None
    amount_cents: int = Field(default=0, description="订单金额")

# 验收业务函数的返回值
class SearchOrdersOutput(ToolOutput):
    orders: list[OrderSummary]
    total: int = Field(ge=0)
