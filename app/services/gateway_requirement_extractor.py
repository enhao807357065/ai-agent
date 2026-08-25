"""从归一化 Gateway 调用推导路由硬性能力需求。"""

from __future__ import annotations

from app.models.capabilities import GatewayRequirements, ModelCapability
from app.models.gateway import GatewayModelCall


def requirements_from_call(call: GatewayModelCall, *, stream: bool) -> GatewayRequirements:
    required = {ModelCapability.CHAT}
    if stream:
        required.add(ModelCapability.STREAMING)
    if call.tools:
        required.add(ModelCapability.TOOL_CALLING)
    if call.response_format:
        response_type = call.response_format.get("type")
        if response_type == "json_schema":
            required.add(ModelCapability.JSON_SCHEMA)
        elif response_type == "json_object":
            required.add(ModelCapability.JSON_OBJECT)
    return GatewayRequirements(capabilities=required, min_output_tokens=call.max_tokens)
