"""
应用配置管理
通过环境变量 + .env 文件加载配置
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录 .env
_project_root = Path(__file__).resolve().parents[2]  # app/core -> ai-agent/
load_dotenv(dotenv_path=_project_root / ".env")


class Settings:
    """集中管理配置项"""

    # Provider 选择: talai | deepseek
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "talai")

    # TAL AI 网关配置（LLM_PROVIDER=talai 时使用）
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://ai-service.tal.com/openai-compatible/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-pro")

    # DeepSeek 原厂配置（LLM_PROVIDER=deepseek 时使用，走 Anthropic API 格式）
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    DEEPSEEK_RESPONSES_BASE_URL: str = os.getenv("DEEPSEEK_RESPONSES_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    DEEPSEEK_ENABLE_THINKING: bool = os.getenv("DEEPSEEK_ENABLE_THINKING", "false").lower() == "true"
    DEEPSEEK_THINKING_BUDGET: int = int(os.getenv("DEEPSEEK_THINKING_BUDGET", "10000"))
    DEEPSEEK_REASONING_EFFORT: str = os.getenv("DEEPSEEK_REASONING_EFFORT", "medium")

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

    # 模型限流配置（JSON 格式，按模型名设置 RPM/TPM）
    # 格式: {"model_name": {"rpm": 60, "tpm": 100000, "max_wait": 60}}
    MODEL_RATE_LIMITS: str = os.getenv("MODEL_RATE_LIMITS", json.dumps({
        "deepseek-reasoner": {"rpm": 60, "tpm": 100000},
        "deepseek-v4-pro": {"rpm": 120, "tpm": 200000},
    }))


settings = Settings()

