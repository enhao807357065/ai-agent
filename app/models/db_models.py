"""
数据库 ORM 模型

表结构：
    runs        - Run 会话记录
    messages    - 对话消息历史
    checkpoints - Agent loop 执行检查点（断点恢复用）

设计原则：
    - 不使用数据库层外键约束（保持灵活性，便于将来分库分表/微服务拆分）
    - 关联关系仅在 ORM 层声明，用于代码便利
    - 数据一致性由业务层保证
"""

from __future__ import annotations

import time

from sqlalchemy import (
    String, Text, Float, Integer, Index, BigInteger
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RunRecord(Base):
    """Run 会话记录"""
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    max_turns: Mapped[int] = mapped_column(Integer, default=10)
    total_turns: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="JSON 序列化的工具定义")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time, onupdate=time.time)

    # ORM 关联（无 DB 层外键，手动指定 foreign_keys）
    messages: Mapped[list["MessageRecord"]] = relationship(
        back_populates="run",
        primaryjoin="RunRecord.run_id == MessageRecord.run_id",
        foreign_keys="MessageRecord.run_id",
        order_by="MessageRecord.seq",
    )
    checkpoints: Mapped[list["CheckpointRecord"]] = relationship(
        back_populates="run",
        primaryjoin="RunRecord.run_id == CheckpointRecord.run_id",
        foreign_keys="CheckpointRecord.run_id",
        order_by="CheckpointRecord.created_at",
    )

    __table_args__ = (
        Index("idx_runs_status", "status"),
        Index("idx_runs_created_at", "created_at"),
    )


class MessageRecord(Base):
    """对话消息记录"""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="消息在对话中的序号")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="assistant 的 tool_calls JSON")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    # ORM 关联（无 DB 层外键）
    run: Mapped["RunRecord"] = relationship(
        back_populates="messages",
        primaryjoin="MessageRecord.run_id == RunRecord.run_id",
        foreign_keys="[MessageRecord.run_id]",
    )

    __table_args__ = (
        Index("idx_messages_run_seq", "run_id", "seq"),
    )


class CheckpointRecord(Base):
    """
    Agent Loop 检查点

    用途：
    - Run 执行中途失败时，记录当前状态用于恢复
    - 记录每个 turn 结束时的快照
    """
    __tablename__ = "checkpoints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False, comment="第几轮")
    status: Mapped[str] = mapped_column(String(32), nullable=False, comment="turn 结束时状态")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="当前消息总数")
    pending_tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True, comment="未完成的 tool_calls JSON")
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="额外元数据")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    # ORM 关联（无 DB 层外键）
    run: Mapped["RunRecord"] = relationship(
        back_populates="checkpoints",
        primaryjoin="CheckpointRecord.run_id == RunRecord.run_id",
        foreign_keys="[CheckpointRecord.run_id]",
    )

    __table_args__ = (
        Index("idx_checkpoints_run_turn", "run_id", "turn"),
    )
