"""
模型独立限流器 — 内存版滑动窗口

每个模型维护独立的 RPM（请求/分钟）和 TPM（Token/分钟）两个滑动窗口。
调用方在请求前 acquire_request()，请求后 report_tokens()。

设计要点：
    - 滑动窗口算法：严格匹配 API 提供商的限流逻辑
    - asyncio.Lock 保证并发安全
    - 超时机制：等待超过 max_wait 秒抛 RateLimitExceeded
    - 单例模式：全局一个 rate_limiter 实例
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


class RateLimitExceeded(Exception):
    """限流等待超时"""

    def __init__(self, model_key: str, dimension: str, usage: int, limit: int, wait_needed: float):
        self.model_key = model_key
        self.dimension = dimension
        self.usage = usage
        self.limit = limit
        self.wait_needed = wait_needed
        # 不在 __init__ 固化消息，str() 时动态生成（因为上层可能修改 model_key/dimension）
        super().__init__()

    def __str__(self) -> str:
        return (
            f"Rate limit exceeded for {self.model_key} ({self.dimension}): "
            f"{self.usage}/{self.limit}, need to wait {self.wait_needed:.1f}s"
        )


@dataclass
class ModelRateLimit:
    """单个模型的限流配置"""
    rpm: int = 60           # 每分钟最大请求数
    tpm: int = 100_000      # 每分钟最大 token 数
    max_wait: float = 5.0  # 最大等待秒数，超时抛异常


class SlidingWindowLimiter:
    """
    滑动窗口限流器

    原理：维护一个 deque 记录窗口内每次消耗的 (timestamp, cost)。
    acquire 时检查窗口内总 cost 是否超限：
        - 未超 → 记录并放行
        - 已超 → sleep 到最早记录过期，再重试
        - cost > limit（单次超限）→ 直接放行并记录，避免死循环
    """

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self._limit = limit
        self._window = window_seconds
        self._records: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _cleanup(self, now: float) -> None:
        """清理窗口外的过期记录"""
        cutoff = now - self._window
        while self._records and self._records[0][0] <= cutoff:
            self._records.popleft()

    @property
    def current_usage(self) -> int:
        """当前窗口内的总用量（非线程安全，仅用于监控）"""
        self._cleanup(time.monotonic())
        return sum(cost for _, cost in self._records)

    @property
    def limit(self) -> int:
        return self._limit

    def record(self, cost: int) -> None:
        """
        非阻塞记录：只写入 deque，不检查配额。
        用于 report_tokens —— token 已经消耗了，不能退回去。
        """
        now = time.monotonic()
        self._records.append((now, cost))

    async def acquire(self, cost: int = 1, max_wait: float = 5.0) -> float:
        """
        获取配额。

        Args:
            cost: 本次消耗的配额数（RPM 为 1，TPM 为 token 数）
            max_wait: 最大等待秒数

        Returns:
            实际等待的秒数

        Raises:
            RateLimitExceeded: 等待超时
        """
        # 防御：单次 cost 超过 limit，直接放行并记录
        # 否则 usage + cost > limit 永远为 True → 死循环
        if cost > self._limit:
            logger.warning(
                "rate_limiter.cost_exceeds_limit",
                cost=cost,
                limit=self._limit,
            )
            async with self._lock:
                self._records.append((time.monotonic(), cost))
            return 0.0

        total_waited = 0.0

        async with self._lock:
            while True:
                now = time.monotonic()
                self._cleanup(now)
                usage = sum(cost_val for _, cost_val in self._records)

                if usage + cost <= self._limit:
                    # 有配额，记录并放行
                    self._records.append((now, cost))
                    return total_waited

                # 没配额 — 计算需要等多久（最早记录过期时间）
                oldest_time = self._records[0][0]
                wait = (oldest_time + self._window) - now + 0.01  # +10ms 余量

                if total_waited + wait > max_wait:
                    raise RateLimitExceeded(
                        model_key="",  # 由上层填充
                        dimension="",
                        usage=usage,
                        limit=self._limit,
                        wait_needed=wait,
                    )

                # 释放锁等待，让其他协程也能执行
                # 注意：这里暂时 hold 锁等待，保证串行获取配额的公平性
                await asyncio.sleep(wait)
                total_waited += wait


class ModelRateLimiter:
    """
    为每个模型维护独立的 RPM + TPM 限流器。

    Usage:
        rate_limiter.configure("deepseek-reasoner", ModelRateLimit(rpm=60, tpm=100000))

        await rate_limiter.acquire_request("deepseek-reasoner")
        # ... 调用模型 ...
        await rate_limiter.report_tokens("deepseek-reasoner", input_tokens + output_tokens)
    """

    def __init__(self):
        self._limiters: dict[str, tuple[SlidingWindowLimiter, SlidingWindowLimiter]] = {}
        self._configs: dict[str, ModelRateLimit] = {}

    def configure(self, model_key: str, config: ModelRateLimit) -> None:
        """注册/更新模型限流配置"""
        self._configs[model_key] = config
        self._limiters[model_key] = (
            SlidingWindowLimiter(config.rpm, 60.0),    # RPM 窗口
            SlidingWindowLimiter(config.tpm, 60.0),    # TPM 窗口
        )
        logger.info(
            "rate_limiter.configured",
            model=model_key,
            rpm=config.rpm,
            tpm=config.tpm,
            max_wait=config.max_wait,
        )

    def _get_or_create(self, model_key: str) -> tuple[ModelRateLimit, SlidingWindowLimiter, SlidingWindowLimiter]:
        """获取限流器，未配置的模型使用默认限制"""
        if model_key not in self._limiters:
            default_config = ModelRateLimit()
            self.configure(model_key, default_config)
            logger.debug("rate_limiter.default_created", model=model_key)
        config = self._configs[model_key]
        rpm_limiter, tpm_limiter = self._limiters[model_key]
        return config, rpm_limiter, tpm_limiter

    async def acquire_request(self, model_key: str) -> float:
        """
        请求前调用：消耗 1 RPM 配额，同时检查 TPM 窗口是否过载。

        Returns:
            实际等待的秒数（0 表示未等待）

        Raises:
            RateLimitExceeded: 等待超时
        """
        config, rpm_limiter, tpm_limiter = self._get_or_create(model_key)
        total_waited = 0.0

        # 1) RPM 限流
        try:
            waited = await rpm_limiter.acquire(cost=1, max_wait=config.max_wait)
            total_waited += waited
        except RateLimitExceeded as e:
            e.model_key = model_key
            e.dimension = "RPM"
            raise

        # 2) TPM 检查：如果窗口内 token 用量已达上限，等待到有空间
        tpm_limiter._cleanup(time.monotonic())
        tpm_usage = sum(cost for _, cost in tpm_limiter._records)
        if tpm_usage >= tpm_limiter.limit:
            try:
                # 用 cost=1 尝试获取（等待窗口腾出空间）
                waited = await tpm_limiter.acquire(cost=1, max_wait=config.max_wait)
                total_waited += waited
            except RateLimitExceeded as e:
                e.model_key = model_key
                e.dimension = "TPM"
                raise

        if total_waited > 0:
            logger.info(
                "rate_limiter.request_waited",
                model=model_key,
                waited_ms=round(total_waited * 1000),
            )

        return total_waited

    def report_tokens(self, model_key: str, tokens: int) -> None:
        """
        请求后调用：记录 token 用量到 TPM 窗口。

        非阻塞 —— token 已经消耗了，无法退回。
        记录后，下一次 acquire_request 会检查 TPM 窗口并自然限速。
        """
        if tokens <= 0:
            return

        _, _, tpm_limiter = self._get_or_create(model_key)
        tpm_limiter.record(tokens)
        logger.debug(
            "rate_limiter.tokens_recorded",
            model=model_key,
            tokens=tokens,
        )

    def get_status(self, model_key: str) -> dict:
        """获取模型限流状态（用于监控/调试接口）"""
        if model_key not in self._limiters:
            return {"configured": False}

        config = self._configs[model_key]
        rpm_limiter, tpm_limiter = self._limiters[model_key]
        return {
            "configured": True,
            "rpm": {"usage": rpm_limiter.current_usage, "limit": config.rpm},
            "tpm": {"usage": tpm_limiter.current_usage, "limit": config.tpm},
        }

    def get_all_status(self) -> dict[str, dict]:
        """获取所有已配置模型的限流状态"""
        return {key: self.get_status(key) for key in self._configs}


# ============================================================
# 全局单例
# ============================================================
rate_limiter = ModelRateLimiter()
