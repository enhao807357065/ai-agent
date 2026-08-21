"""
应用配置管理
通过环境变量 + .env 文件加载配置
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录 .env
_project_root = Path(__file__).resolve().parents[2]  # app/core -> ai-agent/
load_dotenv(dotenv_path=_project_root / ".env")


class Settings:
    """集中管理配置项"""

    # LLM 配置
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://ai-service.tal.com/openai-compatible/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-pro")

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


settings = Settings()

