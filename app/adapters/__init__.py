"""
Adapter 工厂 — 根据 LLM_PROVIDER 环境变量创建对应的 StreamingModel
"""

from app.models.streaming import StreamingModel
from app.core.config import settings


def create_model(model_name: str | None = None) -> StreamingModel:
    """
    根据配置创建 StreamingModel 实例

    LLM_PROVIDER:
        - "talai"    → TAL AI 网关（OpenAI 兼容格式）
        - "deepseek" → DeepSeek 原厂 Anthropic API 格式
    """
    provider = settings.LLM_PROVIDER
    model = model_name or settings.LLM_MODEL

    if provider == "deepseek":
        from app.adapters.deepseek_adapter import DeepSeekAnthropicModel
        return DeepSeekAnthropicModel(
            api_key=settings.DEEPSEEK_API_KEY,
            model=model,
            enable_thinking=settings.DEEPSEEK_ENABLE_THINKING,
            thinking_budget_tokens=settings.DEEPSEEK_THINKING_BUDGET,
        )
    elif provider == "talai":
        from app.adapters.talai_adapter import TalAIStreamingModel
        return TalAIStreamingModel(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            model=model,
            extra_body={"thinking": {"type": "disabled"}},
        )
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER: '{provider}'. "
            f"Supported: 'talai', 'deepseek'"
        )
