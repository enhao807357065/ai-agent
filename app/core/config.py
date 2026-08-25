"""
应用配置管理
通过环境变量 + .env 文件加载配置
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import TypeAdapter, ValidationError

from app.models.capabilities import ModelCapability, RoutingPolicy, TargetProfile
from app.services.adapter_registry import validate_target_profile
from app.models.gateway import ModelRoute

# 加载项目根目录 .env
_project_root = Path(__file__).resolve().parents[2]  # app/core -> ai-agent/
load_dotenv(dotenv_path=_project_root / ".env")


class Settings:
    """集中管理配置项"""

    # Provider 选择: talai | deepseek
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "talai")

    # TAL AI 网关配置（LLM_PROVIDER=talai 时使用）
    TALAI_API_KEY: str = os.getenv("TALAI_API_KEY", "")
    TALAI_BASE_URL: str = os.getenv("TALAI_BASE_URL", "http://ai-service.tal.com/openai-compatible/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-pro")

    # DeepSeek 原厂配置（LLM_PROVIDER=deepseek 时使用，走 Anthropic API 格式）
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    DEEPSEEK_RESPONSES_BASE_URL: str = os.getenv("DEEPSEEK_RESPONSES_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    DEEPSEEK_ENABLE_THINKING: bool = os.getenv("DEEPSEEK_ENABLE_THINKING", "false").lower() == "true"
    DEEPSEEK_THINKING_BUDGET: int = int(os.getenv("DEEPSEEK_THINKING_BUDGET", "10000"))
    DEEPSEEK_REASONING_EFFORT: str = os.getenv("DEEPSEEK_REASONING_EFFORT", "medium")

    # Gateway Phase 1-3：Target Registry（能力、成本、优先级、限制）与路由策略分离。
    # 真实连接信息仍由各 provider 的环境变量提供，密钥绝不进入此 JSON。
    GATEWAY_TARGET_REGISTRY: str = os.getenv("GATEWAY_TARGET_REGISTRY", json.dumps({
        "talai/deepseek-v4-pro": {
            "id": "talai/deepseek-v4-pro", "provider": "talai", "model": "deepseek-v4-pro",
            "capabilities": ["chat", "streaming", "tool_calling", "json_object"],
            "priority": 20, "input_cost_per_million": "2.0", "output_cost_per_million": "8.0",
            "max_output_tokens": 8192,
        },
        "talai/deepseek-v4-flash": {
            "id": "talai/deepseek-v4-flash", "provider": "talai", "model": "deepseek-v4-flash",
            "capabilities": ["chat", "streaming", "tool_calling", "json_object"],
            "priority": 10, "input_cost_per_million": "0.5", "output_cost_per_million": "2.0",
            "max_output_tokens": 8192,
        },
        "deepseek/deepseek-v4-flash": {
            "id": "deepseek/deepseek-v4-flash", "provider": "deepseek", "model": "deepseek-v4-flash",
            "capabilities": ["chat", "streaming", "tool_calling", "json_object", "reasoning"],
            "priority": 30, "input_cost_per_million": "4.0", "output_cost_per_million": "16.0",
            "max_output_tokens": 8192, "enable_thinking": True,
        },
        "deepseek_responses/deepseek-v4-flash": {
            "id": "deepseek_responses/deepseek-v4-flash", "provider": "deepseek_responses", "model": "deepseek-v4-flash",
            "capabilities": ["chat", "streaming", "tool_calling", "json_object", "json_schema", "reasoning"],
            "priority": 15, "input_cost_per_million": "1.0", "output_cost_per_million": "4.0",
            "max_output_tokens": 8192, "reasoning_effort": "medium",
        },
    }))
    GATEWAY_ROUTING_POLICIES: str = os.getenv("GATEWAY_ROUTING_POLICIES", json.dumps({
        "chat-default": {
            "target_ids": ["talai/deepseek-v4-pro", "deepseek/deepseek-v4-flash", "deepseek_responses/deepseek-v4-flash"],
        },
        "chat-fast": {
            "target_ids": ["talai/deepseek-v4-flash", "talai/deepseek-v4-pro", "deepseek_responses/deepseek-v4-flash"],
        },
        "reasoning-pro": {
            "target_ids": ["deepseek/deepseek-v4-flash", "deepseek_responses/deepseek-v4-flash"],
            "required_capabilities": ["reasoning"],
        },
    }))
    GATEWAY_CIRCUIT_FAILURE_THRESHOLD: int = int(os.getenv("GATEWAY_CIRCUIT_FAILURE_THRESHOLD", "3"))
    GATEWAY_CIRCUIT_OPEN_SECONDS: float = float(os.getenv("GATEWAY_CIRCUIT_OPEN_SECONDS", "60"))

    @property
    def gateway_target_registry(self) -> dict[str, TargetProfile]:
        try:
            raw = json.loads(self.GATEWAY_TARGET_REGISTRY)
            profiles = TypeAdapter(dict[str, TargetProfile]).validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"Invalid GATEWAY_TARGET_REGISTRY: {exc}") from exc
        for key, profile in profiles.items():
            if key != profile.id:
                raise ValueError(f"Target registry key '{key}' must equal target id '{profile.id}'")
            validate_target_profile(profile)
        return profiles

    @property
    def gateway_routing_policies(self) -> dict[str, RoutingPolicy]:
        try:
            raw = json.loads(self.GATEWAY_ROUTING_POLICIES)
            return TypeAdapter(dict[str, RoutingPolicy]).validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"Invalid GATEWAY_ROUTING_POLICIES: {exc}") from exc

    # Legacy primary/fallback configuration: retained only for compatibility with previous exercises.
    # GatewayModelRouter uses gateway_target_registry + gateway_routing_policies.
    GATEWAY_MODEL_ROUTES: str = os.getenv("GATEWAY_MODEL_ROUTES", json.dumps({
        "chat-default": {
            "primary": {"provider": "talai", "model": "deepseek-v4-pro"},
            "fallbacks": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
        },
        "chat-fast": {
            "primary": {"provider": "talai", "model": "deepseek-v4-flash"},
            "fallbacks": [{"provider": "talai", "model": "deepseek-v4-pro"}],
        },
        "reasoning-pro": {
            "primary": {"provider": "deepseek", "model": "deepseek-v4-flash", "enable_thinking": True},
            "fallbacks": [{"provider": "talai", "model": "deepseek-v4-pro"}],
        },
    }))

    @property
    def gateway_model_routes(self) -> dict[str, ModelRoute]:
        """解析并校验逻辑模型路由；配置错误应在服务启动/首次调用时立即暴露。"""
        try:
            raw_routes = json.loads(self.GATEWAY_MODEL_ROUTES)
            return TypeAdapter(dict[str, ModelRoute]).validate_python(raw_routes)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"Invalid GATEWAY_MODEL_ROUTES: {exc}") from exc

    # 服务配置
    HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("APP_PORT", "8000"))
    DEBUG: bool = os.getenv("APP_DEBUG", "true").lower() == "true"

    # Run 存储配置
    RUN_BUFFER_TTL: int = int(os.getenv("RUN_BUFFER_TTL", "3600"))  # 60分钟过期

    # 数据库配置
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "lianghao")
    DB_NAME: str = os.getenv("DB_NAME", "ai_agent")
    DB_ECHO: bool = os.getenv("DB_ECHO", "false").lower() == "true"

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"

    # 重试配置
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    LLM_RETRY_DELAY: float = float(os.getenv("LLM_RETRY_DELAY", "1.0"))

    # Gateway 逻辑模型限流开关。关闭时限流器完全 no-op：不等待、不记账、不创建窗口。
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"

    # Gateway 逻辑模型限流配置（JSON 格式，作用于 Gateway HTTP 与每个 Agent Turn）
    # 格式: {"logical_model": {"rpm": 60, "tpm": 100000, "max_wait": 60}}
    MODEL_RATE_LIMITS: str = os.getenv("MODEL_RATE_LIMITS", json.dumps({
        "chat-default": {"rpm": 60, "tpm": 100000},
        "chat-fast": {"rpm": 120, "tpm": 200000},
        "reasoning-pro": {"rpm": 30, "tpm": 100000},
    }))


settings = Settings()

