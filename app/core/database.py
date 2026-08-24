"""
数据库配置与会话管理

使用 SQLAlchemy 2.0 async 模式 + aiomysql 驱动
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.core.config import settings


# 异步引擎
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)

# 会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


async def get_session() -> AsyncSession:
    """获取数据库会话（用于依赖注入）"""
    async with async_session_factory() as session:
        yield session


async def _ensure_database_exists():
    """确保数据库存在，不存在则自动创建"""
    server_url = (
        f"mysql+aiomysql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/?charset=utf8mb4"
    )
    tmp_engine = create_async_engine(server_url, echo=False)
    async with tmp_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.DB_NAME}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    await tmp_engine.dispose()


async def init_db():
    """创建数据库（如不存在）+ 创建所有表"""
    await _ensure_database_exists()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """关闭连接池"""
    await engine.dispose()
