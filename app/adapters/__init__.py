"""Adapter 工厂：Agent 默认模型与 Gateway 指定上游模型分开创建。"""

from typing import Literal, cast

from app.core.config import settings
from app.models.gateway import ProviderTarget
from app.models.streaming import StreamingModel


def create_model_for_target(target: ProviderTarget) -> StreamingModel:
    """按 Gateway 路由目标显式创建模型，不读取全局 LLM_PROVIDER。"""
    if target.provider == "deepseek":
        from app.adapters.deepseek_adapter import DeepSeekAnthropicModel

        return DeepSeekAnthropicModel(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            model=target.model,
            enable_thinking=(
                target.enable_thinking
                if target.enable_thinking is not None
                else settings.DEEPSEEK_ENABLE_THINKING
            ),
            thinking_budget_tokens=target.thinking_budget_tokens or settings.DEEPSEEK_THINKING_BUDGET,
        )

    if target.provider == "deepseek_responses":
        from app.adapters.deepseek_responses_adapter import DeepSeekResponsesModel

        return DeepSeekResponsesModel(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_RESPONSES_BASE_URL,
            model=target.model,
            reasoning_effort=target.reasoning_effort or settings.DEEPSEEK_REASONING_EFFORT or None,
        )

    if target.provider == "talai":
        from app.adapters.talai_adapter import TalAIStreamingModel

        return TalAIStreamingModel(
            api_key=settings.TALAI_API_KEY,
            base_url=settings.TALAI_BASE_URL,
            model=target.model,
            extra_body={"thinking": {"type": "disabled"}},
        )

    raise ValueError(f"Unsupported provider: {target.provider}")


def create_default_model(model_name: str | None = None) -> StreamingModel:
    """供 Agent Run API 使用，沿用 LLM_PROVIDER / LLM_MODEL 默认配置。"""
    provider = cast(Literal["talai", "deepseek", "deepseek_responses"], settings.LLM_PROVIDER)
    model = model_name or settings.LLM_MODEL
    return create_model_for_target(ProviderTarget(provider=provider, model=model))


# 兼容既有 Agent 路由的调用；Gateway 必须使用 create_model_for_target()。
create_model = create_default_model
