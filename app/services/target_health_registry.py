"""Gateway target 的进程内健康注册表与最小 Circuit Breaker。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.capabilities import HealthStatus, TargetHealth


class TargetHealthRegistry:
    """单进程健康视图；多实例部署时可替换为 Redis 实现。"""

    def __init__(self, failure_threshold: int = 3, open_seconds: float = 60.0) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._health: dict[str, TargetHealth] = {}

    def get(self, target_id: str) -> TargetHealth:
        health = self._health.setdefault(target_id, TargetHealth(target_id=target_id))
        if health.status == HealthStatus.OPEN and health.circuit_open_until:
            if datetime.now(UTC) >= health.circuit_open_until:
                # 到期后允许一个正常候选请求作为 half-open probe；成功后恢复 healthy。
                health.status = HealthStatus.DEGRADED
                health.circuit_open_until = None
        return health.model_copy(deep=True)

    def is_eligible(self, target_id: str) -> bool:
        return self.get(target_id).status != HealthStatus.OPEN

    def record_success(self, target_id: str, latency_ms: float) -> None:
        health = self._health.setdefault(target_id, TargetHealth(target_id=target_id))
        health.success_count += 1
        health.consecutive_failures = 0
        health.status = HealthStatus.HEALTHY
        health.circuit_open_until = None
        alpha = 0.2
        health.latency_ewma_ms = (
            latency_ms
            if health.latency_ewma_ms is None
            else alpha * latency_ms + (1 - alpha) * health.latency_ewma_ms
        )

    def record_retriable_failure(self, target_id: str) -> None:
        health = self._health.setdefault(target_id, TargetHealth(target_id=target_id))
        health.failure_count += 1
        health.consecutive_failures += 1
        if health.consecutive_failures >= self._failure_threshold:
            health.status = HealthStatus.OPEN
            health.circuit_open_until = datetime.now(UTC) + timedelta(seconds=self._open_seconds)
        else:
            health.status = HealthStatus.DEGRADED

    def health_score(self, target_id: str) -> float:
        health = self.get(target_id)
        if health.status == HealthStatus.OPEN:
            return 0.0
        total = health.success_count + health.failure_count
        success_rate = health.success_count / total if total else 1.0
        penalty = 0.15 if health.status == HealthStatus.DEGRADED else 0.0
        return max(0.0, success_rate - penalty)
