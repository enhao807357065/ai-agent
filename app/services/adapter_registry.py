"""Adapter 能力注册表：声明协议适配器可实现的能力上界。"""

from __future__ import annotations

from app.models.capabilities import ModelCapability, TargetProfile

# TargetProfile 的 capabilities 是具体模型的能力子集；不得超出 adapter 的实现上界。
ADAPTER_CAPABILITIES: dict[str, frozenset[ModelCapability]] = {
    "talai": frozenset({
        ModelCapability.CHAT,
        ModelCapability.STREAMING,
        ModelCapability.TOOL_CALLING,
        ModelCapability.JSON_OBJECT,
    }),
    "deepseek": frozenset({
        ModelCapability.CHAT,
        ModelCapability.STREAMING,
        ModelCapability.TOOL_CALLING,
        ModelCapability.JSON_OBJECT,
        ModelCapability.REASONING,
    }),
    "deepseek_responses": frozenset({
        ModelCapability.CHAT,
        ModelCapability.STREAMING,
        ModelCapability.TOOL_CALLING,
        ModelCapability.JSON_OBJECT,
        ModelCapability.JSON_SCHEMA,
        ModelCapability.REASONING,
    }),
}


def validate_target_profile(profile: TargetProfile) -> None:
    adapter_capabilities = ADAPTER_CAPABILITIES[profile.provider]
    unsupported = profile.capabilities - adapter_capabilities
    if unsupported:
        names = ", ".join(sorted(capability.value for capability in unsupported))
        raise ValueError(
            f"Target '{profile.id}' declares capabilities unsupported by "
            f"adapter '{profile.provider}': {names}"
        )
