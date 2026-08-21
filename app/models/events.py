"""
RunEvent 定义 — Agent 执行过程中的 SSE 事件协议

设计思路：
    Agent 执行是一个 Run，Run 由多个 Turn 组成（每轮 LLM 调用 + 可能的工具执行）。
    SSE 流输出的每个事件都是一个 RunEvent，携带统一结构便于前端解析和回放。

事件类型：
    run.created       - Run 创建完成，返回 run_id
    run.in_progress   - Run 开始执行（进入 agent loop）
    text.delta        - LLM 文本流式输出的一个 chunk
    text.done         - 一轮 LLM 文本输出完毕
    tool.calling      - Agent 决定调用工具
    tool.result       - 工具执行结果返回
    run.completed     - Run 正常结束，附带最终结果
    run.failed        - Run 执行失败
    run.cancelled     - Run 被取消
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """SSE 事件类型枚举"""
    RUN_CREATED = "run.created"
    RUN_IN_PROGRESS = "run.in_progress"
    TEXT_DELTA = "text.delta"
    TEXT_DONE = "text.done"
    TOOL_CALLING = "tool.calling"
    TOOL_RESULT = "tool.result"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class RunEvent(BaseModel):
    """
    统一的 SSE 事件结构

    所有通过 SSE 推送的事件都遵循此格式，前端只需解析一种结构。
    """
    event: EventType = Field(..., description="事件类型")
    run_id: str = Field(..., description="所属 Run ID")
    data: dict[str, Any] = Field(default_factory=dict, description="事件携带的数据")
    timestamp: float = Field(default_factory=time.time, description="事件产生时间戳")
    sequence: int = Field(default=0, description="事件序号（用于断线重连定位）")

    def to_sse(self) -> str:
        """格式化为 SSE 协议字符串"""
        import json
        payload = self.model_dump_json(exclude_none=True)
        return f"id: {self.sequence}\nevent: {self.event.value}\ndata: {payload}\n\n"
