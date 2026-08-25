"""
Run Store — Agent Run 的生命周期管理

职责：
    1. 存储 Run 的状态、事件历史
    2. 提供 Run 的创建/查询/取消接口
    3. 支持 SSE 订阅（新事件通知）

当前实现：内存存储（进程重启丢失）
未来可替换为 Redis / PostgreSQL 持久化实现
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.models.events import RunEvent, EventType
from app.models.schemas import RunStatus


class RunState:
    """
    单个 Run 的完整状态

    Run = 会话。一个 Run 可以多次执行 agent_loop（用户追加消息后继续对话）。
    """

    def __init__(self, run_id: str, model: str, system: str | None = None):
        self.run_id = run_id
        self.model = model
        self.status: RunStatus = RunStatus.CREATED
        self.created_at: float = time.time()
        self.completed_at: float | None = None
        self.total_turns: int = 0
        self.error: str | None = None

        # 对话历史（LLM 格式，完整保存）
        self.messages: list[dict] = []
        if system:
            self.messages.append({"role": "system", "content": system})

        # 工具定义（创建时设置，后续复用）
        self.tools: list[dict] | None = None
        self.temperature: float = 0.7
        self.max_turns: int = 10
        self.max_tokens: int = 4096
        self.response_format: dict[str, Any] | None = None

        # 事件历史（有序）
        self.events: list[RunEvent] = []
        self._sequence: int = 0

        # 通知机制：有新事件时唤醒等待的订阅者
        self._notify_event: asyncio.Event = asyncio.Event()

        # 后台任务引用（用于取消）
        self.task: asyncio.Task | None = None

    def append_event(self, event_type: EventType, data: dict[str, Any] | None = None) -> RunEvent:
        """追加一个事件并通知订阅者"""
        self._sequence += 1
        event = RunEvent(
            event=event_type,
            run_id=self.run_id,
            data=data or {},
            sequence=self._sequence,
        )
        self.events.append(event)
        # 唤醒所有等待中的订阅者
        self._notify_event.set()
        self._notify_event.clear()
        return event

    def mark_completed(self):
        self.status = RunStatus.COMPLETED
        self.completed_at = time.time()

    def mark_failed(self, error: str):
        self.status = RunStatus.FAILED
        self.completed_at = time.time()
        self.error = error

    def mark_rate_limited(self, error: str):
        self.status = RunStatus.RATE_LIMITED
        self.completed_at = time.time()
        self.error = error

    def mark_cancelled(self):
        self.status = RunStatus.CANCELLED
        self.completed_at = time.time()

    @property
    def is_terminal(self) -> bool:
        """Run 是否已结束（不会再产生新事件）"""
        return self.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.RATE_LIMITED, RunStatus.CANCELLED)


class RunStore:
    """
    Run 存储管理器（内存实现）

    线程安全：所有操作在同一事件循环中执行，无需加锁。
    """

    def __init__(self, ttl_seconds: int = 600):
        self._runs: dict[str, RunState] = {}
        self._ttl = ttl_seconds

    def create(self, run_id: str, model: str, system: str | None = None) -> RunState:
        """创建一个新 Run"""
        state = RunState(run_id=run_id, model=model, system=system)
        self._runs[run_id] = state
        return state

    def get(self, run_id: str) -> RunState | None:
        """获取 Run 状态"""
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 20) -> list[RunState]:
        """列出最近的 Run（按创建时间倒序）"""
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def cancel(self, run_id: str) -> bool:
        """
        取消一个 Run

        Returns:
            True 如果成功取消，False 如果 Run 不存在或已结束
        """
        state = self._runs.get(run_id)
        if state is None or state.is_terminal:
            return False

        # 取消后台任务
        if state.task and not state.task.done():
            state.task.cancel()

        state.mark_cancelled()
        state.append_event(EventType.RUN_CANCELLED, {"reason": "user_cancelled"})
        return True

    def cleanup_expired(self) -> int:
        """清理过期的已完成 Run，返回清理数量"""
        now = time.time()
        expired_ids = [
            rid for rid, state in self._runs.items()
            if state.is_terminal
            and state.completed_at is not None
            and (now - state.completed_at) > self._ttl
        ]
        for rid in expired_ids:
            del self._runs[rid]
        return len(expired_ids)


# 全局单例
run_store = RunStore()
