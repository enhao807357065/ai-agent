"""
数据库持久化服务 — Run / Message / Checkpoint 的 CRUD

职责：
    1. 将内存 RunState 持久化到 MySQL
    2. 从 MySQL 恢复 RunState（服务重启后）
    3. 管理 Checkpoint（每轮 turn 结束写入）
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.db_models import RunRecord, MessageRecord, CheckpointRecord

logger = structlog.get_logger(__name__)


class DBService:
    """数据库持久化操作"""

    # ============================
    # Run CRUD
    # ============================

    async def save_run(self, run_id: str, model: str, status: str,
                       system_prompt: str | None = None,
                       temperature: float = 0.7,
                       max_turns: int = 10,
                       tools: list[dict] | None = None) -> None:
        """创建或更新 Run 记录"""
        async with async_session_factory() as session:
            # session.begin() 是一个事务 context manager：
            # - 正常退出 async with → 自动 commit()
            # - 抛异常退出 → 自动 rollback()
            #
            # 这是 SQLAlchemy 2.0 推荐的写法
            async with session.begin():
                existing = await session.get(RunRecord, run_id)
                if existing:
                    # 继续会话可切换逻辑模型，因此同时更新持久化的逻辑模型名。
                    existing.model = model
                    existing.status = status
                    existing.updated_at = time.time()
                    if tools is not None:
                        existing.tools_json = json.dumps(tools, ensure_ascii=False)
                else:
                    record = RunRecord(
                        run_id=run_id,
                        model=model,
                        status=status,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_turns=max_turns,
                        tools_json=json.dumps(tools, ensure_ascii=False) if tools else None,
                    )
                    session.add(record)
        logger.debug("db.run_saved", run_id=run_id, status=status)

    async def update_run_status(self, run_id: str, status: str,
                                total_turns: int | None = None,
                                error: str | None = None) -> None:
        """更新 Run 状态"""
        async with async_session_factory() as session:
            async with session.begin():
                values: dict[str, Any] = {
                    "status": status,
                    "updated_at": time.time(),
                }
                if status in ("completed", "failed", "cancelled"):
                    values["completed_at"] = time.time()
                if total_turns is not None:
                    values["total_turns"] = total_turns
                if error is not None:
                    values["error"] = error

                await session.execute(
                    update(RunRecord).where(RunRecord.run_id == run_id).values(**values)
                )
        logger.debug("db.run_status_updated", run_id=run_id, status=status)

    async def get_run(self, run_id: str) -> RunRecord | None:
        """获取 Run 记录"""
        async with async_session_factory() as session:
            return await session.get(RunRecord, run_id)

    async def list_runs(self, limit: int = 20) -> list[RunRecord]:
        """列出最近的 Run"""
        async with async_session_factory() as session:
            result = await session.execute(
                select(RunRecord).order_by(RunRecord.created_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    # ============================
    # Message CRUD
    # ============================

    async def save_message(self, run_id: str, seq: int, role: str,
                           content: str | None = None,
                           tool_call_id: str | None = None,
                           tool_calls: list[dict] | None = None) -> None:
        """保存单条消息"""
        async with async_session_factory() as session:
            async with session.begin():
                record = MessageRecord(
                    run_id=run_id,
                    seq=seq,
                    role=role,
                    content=content,
                    tool_call_id=tool_call_id,
                    tool_calls_json=json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                )
                session.add(record)
        logger.debug("db.message_saved", run_id=run_id, seq=seq, role=role)

    async def save_messages_batch(self, run_id: str, messages: list[dict], start_seq: int = 0) -> None:
        """批量保存消息"""
        async with async_session_factory() as session:
            async with session.begin():
                for i, msg in enumerate(messages):
                    record = MessageRecord(
                        run_id=run_id,
                        seq=start_seq + i,
                        role=msg["role"],
                        content=msg.get("content"),
                        tool_call_id=msg.get("tool_call_id"),
                        tool_calls_json=json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None,
                    )
                    session.add(record)
        logger.debug("db.messages_batch_saved", run_id=run_id, count=len(messages))

    async def get_messages(self, run_id: str) -> list[MessageRecord]:
        """获取 Run 的所有消息（按 seq 排序）"""
        async with async_session_factory() as session:
            result = await session.execute(
                select(MessageRecord)
                .where(MessageRecord.run_id == run_id)
                .order_by(MessageRecord.seq)
            )
            return list(result.scalars().all())

    # ============================
    # Checkpoint CRUD
    # ============================

    async def save_checkpoint(self, run_id: str, turn: int, status: str,
                              message_count: int,
                              pending_tool_calls: list[dict] | None = None,
                              metadata: dict | None = None) -> None:
        """保存检查点"""
        async with async_session_factory() as session:
            async with session.begin():
                record = CheckpointRecord(
                    run_id=run_id,
                    turn=turn,
                    status=status,
                    message_count=message_count,
                    pending_tool_calls=json.dumps(pending_tool_calls, ensure_ascii=False) if pending_tool_calls else None,
                    metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
                )
                session.add(record)
        logger.debug("db.checkpoint_saved", run_id=run_id, turn=turn)

    async def get_latest_checkpoint(self, run_id: str) -> CheckpointRecord | None:
        """获取最新检查点"""
        async with async_session_factory() as session:
            result = await session.execute(
                select(CheckpointRecord)
                .where(CheckpointRecord.run_id == run_id)
                .order_by(CheckpointRecord.turn.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def get_checkpoints(self, run_id: str) -> list[CheckpointRecord]:
        """获取所有检查点"""
        async with async_session_factory() as session:
            result = await session.execute(
                select(CheckpointRecord)
                .where(CheckpointRecord.run_id == run_id)
                .order_by(CheckpointRecord.turn)
            )
            return list(result.scalars().all())


# 全局单例
db_service = DBService()
