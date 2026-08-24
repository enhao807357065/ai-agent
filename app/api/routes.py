"""
API 路由 — Run 的创建/订阅/取消

接口设计：
    POST   /v1/runs           创建 Run（启动 agent loop，返回 run_id）
    GET    /v1/runs/{id}/stream  订阅 Run 的 SSE 事件流
    POST   /v1/runs/{id}/cancel  取消 Run
    GET    /v1/runs/{id}       获取 Run 状态
    GET    /v1/runs/{id}/messages  获取完整对话历史
    GET    /v1/runs/{id}/checkpoints  获取检查点列表
    POST   /v1/runs/{id}/resume  从检查点恢复执行
    GET    /v1/runs            列出最近的 Run
    GET    /health             健康检查
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.events import EventType
from app.models.schemas import (
    CreateRunRequest,
    RunInfo,
    RunStatus,
    CancelRunResponse,
)
from app.services.run_store import run_store, RunState
from app.services.agent_loop import agent_loop
from app.services.db_service import db_service
from app.adapters import create_model

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1")


# ============================================================
# 模型工厂（委托给 adapters 包的 create_model）
# ============================================================

def _create_model(model_name: str | None = None):
    """创建模型实例（根据 LLM_PROVIDER 环境变量自动选择 adapter）"""
    return create_model(model_name)


# ============================================================
# 内置工具执行器（demo 用）
# ============================================================

async def _demo_tool_executor(name: str, arguments: dict) -> str:
    """
    演示用的工具执行器
    实际项目中应该替换为真正的工具注册表
    """
    if name == "get_weather":
        city = arguments.get("city", "北京")
        await asyncio.sleep(0.5)  # 模拟 IO
        return json.dumps({"city": city, "weather": "晴", "temperature": 28}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)


# ============================================================
# API 路由
# ============================================================

@router.post("/runs")
async def create_run(req: CreateRunRequest):
    """
    创建 / 继续 Run

    - 不传 run_id → 新建会话，返回新 run_id
    - 传 run_id → 在已有会话上追加消息并继续执行 agent loop
    """
    model_name = req.model or settings.LLM_MODEL
    request_time = time.time()

    # ---- 新建 or 继续 ----
    if req.run_id:
        # 继续已有会话
        state = run_store.get(req.run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Run {req.run_id} not found")

        # 检查是否还有正在执行的 task
        if state.task and not state.task.done():
            raise HTTPException(
                status_code=409,
                detail=f"Run {req.run_id} is still in progress, wait for completion or cancel first",
            )

        run_id = req.run_id

        # 追加用户新消息到对话历史
        msg_start_seq = len(state.messages)
        for msg in req.messages:
            m = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            state.messages.append(m)

        # 持久化新消息
        new_msgs = [{"role": msg.role, "content": msg.content, "tool_call_id": msg.tool_call_id} for msg in req.messages]
        await db_service.save_messages_batch(run_id, new_msgs, start_seq=msg_start_seq)

        # 更新工具定义（如果传了）
        if req.tools:
            state.tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in req.tools
            ]

        # 重置状态为新一轮执行
        state.status = RunStatus.CREATED
        state.completed_at = None
        state.error = None
        state.temperature = req.temperature
        state.max_turns = req.max_turns

        logger.info(
            "api.run_continue",
            run_id=run_id,
            msg_count=len(state.messages),
            request_duration_ms=round((time.time() - request_time) * 1000),
        )

    else:
        # 新建会话
        run_id = str(uuid.uuid4())
        state = run_store.create(run_id=run_id, model=model_name, system=req.system)

        # 追加用户消息
        for msg in req.messages:
            m = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            state.messages.append(m)

        # 保存工具定义
        if req.tools:
            state.tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in req.tools
            ]

        state.temperature = req.temperature
        state.max_turns = req.max_turns

        # ---- 持久化到数据库 ----
        await db_service.save_run(
            run_id=run_id,
            model=model_name,
            status="created",
            system_prompt=req.system,
            temperature=req.temperature,
            max_turns=req.max_turns,
            tools=state.tools,
        )
        await db_service.save_messages_batch(run_id, state.messages, start_seq=0)

        logger.info(
            "api.run_created",
            run_id=run_id,
            model=model_name,
            message_count=len(state.messages),
            has_tools=bool(req.tools),
            request_duration_ms=round((time.time() - request_time) * 1000),
        )

    # ---- 发射 created 事件 ----
    state.append_event(EventType.RUN_CREATED, {"model": model_name})

    # ---- 创建模型实例 & 启动 agent loop ----
    model = _create_model(model_name)

    if req.stream:
        # 流式模式：后台启动 agent loop，客户端通过 SSE 订阅
        task = asyncio.create_task(
            agent_loop(
                run_state=state,
                model=model,
                messages=state.messages,
                tools=state.tools,
                tool_executor=_demo_tool_executor if state.tools else None,
                temperature=state.temperature,
                max_turns=state.max_turns,
                stream=True,
            )
        )
        state.task = task

        return {"run_id": run_id, "status": "created", "last_event_id": state._sequence}
    else:
        # 非流式模式：同步等待完整结果返回
        await agent_loop(
            run_state=state,
            model=model,
            messages=state.messages,
            tools=state.tools,
            tool_executor=_demo_tool_executor if state.tools else None,
            temperature=state.temperature,
            max_turns=state.max_turns,
            stream=False,
        )

        # 提取最后一条 assistant 消息作为响应
        assistant_messages = [m for m in state.messages if m.get("role") == "assistant"]
        last_assistant = assistant_messages[-1] if assistant_messages else {}

        return {
            "run_id": run_id,
            "status": state.status.value,
            "message": {
                "role": "assistant",
                "content": last_assistant.get("content", ""),
                "tool_calls": last_assistant.get("tool_calls"),
            },
            "total_turns": state.total_turns,
        }


@router.get("/runs/{run_id}/stream")
async def subscribe_run(request: Request, run_id: str, last_event_id: int = 0):
    """
    订阅 Run 的 SSE 事件流

    支持断线重连：通过 last_event_id 参数指定从哪个序号之后开始推送。
    """
    state = run_store.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    async def _event_stream() -> AsyncGenerator[str, None]:
        """从 RunState 的事件列表中流式推送"""
        current_seq = last_event_id

        while True:
            # 检查客户端是否断开
            if await request.is_disconnected():
                return

            # 推送所有已有但未发送的事件
            for event in state.events:
                if event.sequence > current_seq:
                    yield event.to_sse()
                    current_seq = event.sequence

            # 如果 Run 已结束，关闭流
            if state.is_terminal:
                return

            # 等待新事件（带超时，定期检查断连）
            try:
                await asyncio.wait_for(state._notify_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """取消 Run"""
    success = run_store.cancel(run_id)
    if not success:
        state = run_store.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} already in terminal state: {state.status.value}",
        )

    # 持久化取消状态
    await db_service.update_run_status(run_id, "cancelled")

    logger.info("api.run_cancelled", run_id=run_id)

    return CancelRunResponse(
        run_id=run_id,
        status=RunStatus.CANCELLED,
        message="Run cancelled successfully",
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """获取 Run 详情（内存优先，fallback DB）"""
    state = run_store.get(run_id)
    if state:
        return RunInfo(
            run_id=state.run_id,
            status=state.status,
            created_at=state.created_at,
            completed_at=state.completed_at,
            model=state.model,
            total_turns=state.total_turns,
            error=state.error,
        )

    # fallback: 从 DB 查
    record = await db_service.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return RunInfo(
        run_id=record.run_id,
        status=RunStatus(record.status),
        created_at=record.created_at,
        completed_at=record.completed_at,
        model=record.model,
        total_turns=record.total_turns,
        error=record.error,
    )


@router.get("/runs/{run_id}/messages")
async def get_messages(run_id: str):
    """获取 Run 的完整对话历史（内存优先，fallback DB）"""
    state = run_store.get(run_id)
    if state:
        return {"run_id": run_id, "messages": state.messages}

    # fallback: 从 DB 查
    msg_records = await db_service.get_messages(run_id)
    if not msg_records:
        # 检查 run 是否存在
        record = await db_service.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    messages = []
    for m in msg_records:
        msg: dict = {"role": m.role}
        if m.content:
            msg["content"] = m.content
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.tool_calls_json:
            msg["tool_calls"] = json.loads(m.tool_calls_json)
        messages.append(msg)

    return {"run_id": run_id, "messages": messages}


@router.get("/runs/{run_id}/checkpoints")
async def get_checkpoints(run_id: str):
    """获取 Run 的所有检查点"""
    checkpoints = await db_service.get_checkpoints(run_id)
    if not checkpoints:
        # 检查 run 是否存在
        state = run_store.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return {
        "run_id": run_id,
        "checkpoints": [
            {
                "id": cp.id,
                "turn": cp.turn,
                "status": cp.status,
                "message_count": cp.message_count,
                "created_at": cp.created_at,
                "metadata": json.loads(cp.metadata_json) if cp.metadata_json else None,
            }
            for cp in checkpoints
        ],
    }


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str):
    """
    从最新检查点恢复 Run 执行

    适用场景：Run 执行到一半服务重启了，从 DB 恢复状态继续。
    """
    # 检查内存中是否已有（还在跑的不能 resume）
    state = run_store.get(run_id)
    if state and state.task and not state.task.done():
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is still in progress",
        )

    # 从 DB 恢复
    run_record = await db_service.get_run(run_id)
    if run_record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found in database")

    if run_record.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is already {run_record.status}, cannot resume",
        )

    # 恢复消息历史
    msg_records = await db_service.get_messages(run_id)
    messages = []
    for m in msg_records:
        msg: dict = {"role": m.role}
        if m.content:
            msg["content"] = m.content
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.tool_calls_json:
            msg["tool_calls"] = json.loads(m.tool_calls_json)
        messages.append(msg)

    # 重建 RunState
    state = run_store.create(
        run_id=run_id,
        model=run_record.model,
        system=None,  # system 已在 messages 里
    )
    state.messages = messages
    state.temperature = run_record.temperature
    state.max_turns = run_record.max_turns
    if run_record.tools_json:
        state.tools = json.loads(run_record.tools_json)

    # 获取最新 checkpoint 确定从哪个 turn 开始
    checkpoint = await db_service.get_latest_checkpoint(run_id)
    if checkpoint:
        state.total_turns = checkpoint.turn

    logger.info(
        "api.run_resume",
        run_id=run_id,
        restored_messages=len(messages),
        resume_from_turn=state.total_turns,
    )

    # 发射事件 & 启动 agent loop
    state.append_event(EventType.RUN_CREATED, {"model": run_record.model, "resumed": True})

    model = _create_model(run_record.model)
    task = asyncio.create_task(
        agent_loop(
            run_state=state,
            model=model,
            messages=state.messages,
            tools=state.tools,
            tool_executor=_demo_tool_executor if state.tools else None,
            temperature=state.temperature,
            max_turns=state.max_turns,
        )
    )
    state.task = task

    return {
        "run_id": run_id,
        "status": "resumed",
        "last_event_id": state._sequence,
        "restored_messages": len(messages),
        "resume_from_turn": state.total_turns,
    }


@router.get("/runs")
async def list_runs(limit: int = 20):
    """列出最近的 Run（合并内存中活跃的 + DB 历史记录）"""
    # 从 DB 查全量（包含历史）
    db_runs = await db_service.list_runs(limit=limit)

    # 内存中活跃但可能还没写完 DB 的（in_progress 状态）
    memory_runs = run_store.list_runs(limit=limit)

    # 合并：以 DB 为主，内存中正在跑的补充进去
    seen_ids = {r.run_id for r in db_runs}
    result = []

    # 先加内存中活跃但 DB 还没有的
    for state in memory_runs:
        if state.run_id not in seen_ids:
            result.append(RunInfo(
                run_id=state.run_id,
                status=state.status,
                created_at=state.created_at,
                completed_at=state.completed_at,
                model=state.model,
                total_turns=state.total_turns,
                error=state.error,
            ))

    # 再加 DB 记录（内存中有活跃状态则用内存的，更实时）
    for record in db_runs:
        state = run_store.get(record.run_id)
        if state:
            result.append(RunInfo(
                run_id=state.run_id,
                status=state.status,
                created_at=state.created_at,
                completed_at=state.completed_at,
                model=state.model,
                total_turns=state.total_turns,
                error=state.error,
            ))
        else:
            result.append(RunInfo(
                run_id=record.run_id,
                status=RunStatus(record.status),
                created_at=record.created_at,
                completed_at=record.completed_at,
                model=record.model,
                total_turns=record.total_turns,
                error=record.error,
            ))

    # 按创建时间倒序，截取 limit
    result.sort(key=lambda r: r.created_at, reverse=True)
    return result[:limit]
