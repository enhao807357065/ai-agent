"""
Agent Loop — 核心执行引擎（增强版）

增强特性：
    1. Gateway 模型调用：逻辑模型/能力路由、熔断及 fallback 由 GatewayModelRouter 统一处理
    2. 详细日志：每步操作记录耗时、token 用量、错误详情
    3. Checkpoint：每轮 turn 结束后持久化状态，支持断点恢复

设计约束：
    - Agent Loop 不知道 HTTP/SSE 的存在，只通过 RunState.append_event 输出
    - 工具执行通过 tool_executor 回调注入，loop 本身不绑定任何具体工具
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import AsyncIterator
from typing import Any, Callable, Awaitable, Protocol

import structlog

from app.core.config import settings
from app.models.events import EventType
from app.models.gateway import (
    GatewayCompletedEvent,
    GatewayModelCall,
    GatewayModelResult,
    GatewayStreamEvent,
    GatewayTextDelta,
    GatewayToolCallEvent,
)
from app.models.streaming import ToolCallChunk
from app.services.run_store import RunState
from app.services.db_service import db_service
from app.services.rate_limiter import RateLimitExceeded

logger = structlog.get_logger(__name__)

# 工具执行器类型：接收 (tool_name, arguments) -> 返回结果字符串
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


class GatewayExecutionPort(Protocol):
    """Agent Loop 依赖的进程内模型执行端口；HTTP Gateway 也复用同一实现。"""

    async def complete(
        self,
        logical_model: str | None,
        call: GatewayModelCall,
    ) -> GatewayModelResult: ...

    def stream(
        self,
        logical_model: str | None,
        call: GatewayModelCall,
    ) -> AsyncIterator[GatewayStreamEvent]: ...


# ============================================================
# Agent Loop 主函数
# ============================================================

async def agent_loop(
    run_state: RunState,
    gateway_router,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_executor: ToolExecutor | None = None,
    temperature: float = 0.7,
    max_turns: int = 10,
    max_tokens: int = 4096,
    response_format: dict[str, Any] | None = None,
    stream: bool = True,
) -> None:
    """
    Agent Loop 主函数（增强版）

    Args:
        run_state: Run 状态对象（事件通过它发射）
        model: 流式模型实例
        messages: 初始消息列表（会被就地修改，追加 assistant/tool 消息）
        tools: 工具定义列表（OpenAI 格式）
        tool_executor: 工具执行回调（为 None 时不执行工具，直接返回）
        temperature: 生成温度
        max_turns: 最大循环轮次
        stream: 是否流式调用模型（False 时使用 complete() 非流式调用）
    """
    from app.models.schemas import RunStatus

    run_state.status = RunStatus.IN_PROGRESS
    run_state.append_event(EventType.RUN_IN_PROGRESS)

    run_id = run_state.run_id
    turn = 0
    full_text = ""
    loop_start_time = time.time()

    logger.info(
        "agent_loop.start",
        run_id=run_id,
        model=run_state.model,
        message_count=len(messages),
        has_tools=tools is not None,
        max_turns=max_turns,
    )

    # 持久化 Run 状态
    await db_service.update_run_status(run_id, "in_progress")

    try:
        while turn < max_turns:
            turn += 1
            turn_start_time = time.time()
            run_state.total_turns = turn

            logger.info(
                "agent_loop.turn_start",
                run_id=run_id,
                turn=turn,
                message_count=len(messages),
            )

            # Gateway 统一处理模型候选 fallback；此处仅负责 Run 生命周期与事件落盘。
            text_parts: list[str] = []
            tool_calls: list[ToolCallChunk] = []
            finish_reason = "stop"
            input_tokens = 0
            output_tokens = 0
            try:
                text_parts, tool_calls, finish_reason, input_tokens, output_tokens, ttft_ms = (
                    await _call_model_via_gateway(
                        gateway_router=gateway_router,
                        logical_model=run_state.model,
                        messages=messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                        run_state=run_state,
                        turn=turn,
                        stream=stream,
                    )
                )
            except RateLimitExceeded as e:
                # 限流超时 — 标记为 rate_limited，区别于普通失败
                error_msg = f"Rate limited: {e}"
                logger.warning(
                    "agent_loop.rate_limited",
                    run_id=run_id,
                    turn=turn,
                    model=e.model_key,
                    dimension=e.dimension,
                    usage=e.usage,
                    limit=e.limit,
                )
                run_state.mark_rate_limited(error_msg)
                run_state.append_event(EventType.RUN_FAILED, {
                    "error": error_msg,
                    "error_type": "rate_limited",
                    "model": e.model_key,
                    "dimension": e.dimension,
                })
                await db_service.update_run_status(run_id, "rate_limited", total_turns=turn, error=error_msg)
                return
            except Exception as e:
                # Gateway 已耗尽允许的候选或遇到不可恢复错误。
                error_msg = f"Gateway model call failed: {type(e).__name__}: {e}"
                logger.error(
                    "agent_loop.llm_call_exhausted",
                    run_id=run_id,
                    turn=turn,
                    error=error_msg,
                    traceback=traceback.format_exc(),
                )
                run_state.mark_failed(error_msg)
                run_state.append_event(EventType.RUN_FAILED, {"error": error_msg})
                await db_service.update_run_status(run_id, "failed", total_turns=turn, error=error_msg)
                return

            # ---- 一轮模型输出完毕 ----
            full_text = "".join(text_parts)
            turn_duration = time.time() - turn_start_time

            logger.info(
                "agent_loop.turn_llm_done",
                run_id=run_id,
                turn=turn,
                text_length=len(full_text),
                tool_call_count=len(tool_calls),
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                ttft_ms=ttft_ms,
                duration_ms=round(turn_duration * 1000),
            )

            if full_text:
                run_state.append_event(EventType.TEXT_DONE, {
                    "content": full_text,
                    "turn": turn,
                })

            # 构造 assistant 消息追加到上下文
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if full_text:
                assistant_msg["content"] = full_text
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            # 持久化 assistant 消息
            await db_service.save_message(
                run_id=run_id,
                seq=len(messages) - 1,
                role="assistant",
                content=full_text or None,
                tool_calls=assistant_msg.get("tool_calls"),
            )

            # ---- 如果没有工具调用，结束循环 ----
            if not tool_calls:
                break

            # ---- 执行工具 ----
            if tool_executor is None:
                for tc in tool_calls:
                    run_state.append_event(EventType.TOOL_CALLING, {
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    })
                break

            for tc in tool_calls:
                tool_start_time = time.time()

                run_state.append_event(EventType.TOOL_CALLING, {
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                })

                logger.info(
                    "agent_loop.tool_call_start",
                    run_id=run_id,
                    turn=turn,
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    arguments=tc.arguments,
                )

                try:
                    result = await _execute_tool_with_retry(tool_executor, tc.name, tc.arguments)
                    tool_duration = time.time() - tool_start_time

                    logger.info(
                        "agent_loop.tool_call_done",
                        run_id=run_id,
                        turn=turn,
                        tool_name=tc.name,
                        result_length=len(result),
                        duration_ms=round(tool_duration * 1000),
                    )
                except Exception as e:
                    result = f"Tool execution error: {type(e).__name__}: {e}"
                    tool_duration = time.time() - tool_start_time
                    logger.error(
                        "agent_loop.tool_call_failed",
                        run_id=run_id,
                        turn=turn,
                        tool_name=tc.name,
                        error=str(e),
                        duration_ms=round(tool_duration * 1000),
                        traceback=traceback.format_exc(),
                    )

                run_state.append_event(EventType.TOOL_RESULT, {
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "result": result,
                })

                # 工具结果追加到上下文
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
                messages.append(tool_msg)

                # 持久化 tool 消息
                await db_service.save_message(
                    run_id=run_id,
                    seq=len(messages) - 1,
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                )

            # ---- Turn 结束，写入 Checkpoint ----
            await db_service.save_checkpoint(
                run_id=run_id,
                turn=turn,
                status="tool_calls_completed",
                message_count=len(messages),
                pending_tool_calls=None,
                metadata={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "turn_duration_ms": round((time.time() - turn_start_time) * 1000),
                },
            )

            logger.info(
                "agent_loop.checkpoint_saved",
                run_id=run_id,
                turn=turn,
                message_count=len(messages),
            )

        # ---- Loop 结束 ----
        total_duration = time.time() - loop_start_time

        run_state.mark_completed()
        run_state.append_event(EventType.RUN_COMPLETED, {
            "final_text": full_text,
            "total_turns": turn,
        })

        await db_service.update_run_status(run_id, "completed", total_turns=turn)

        # 最终 Checkpoint
        await db_service.save_checkpoint(
            run_id=run_id,
            turn=turn,
            status="completed",
            message_count=len(messages),
            metadata={"total_duration_ms": round(total_duration * 1000)},
        )

        logger.info(
            "agent_loop.completed",
            run_id=run_id,
            total_turns=turn,
            total_duration_ms=round(total_duration * 1000),
        )

    except asyncio.CancelledError:
        run_state.mark_cancelled()
        run_state.append_event(EventType.RUN_CANCELLED, {"reason": "task_cancelled"})
        await db_service.update_run_status(run_id, "cancelled", total_turns=turn)

        logger.warning(
            "agent_loop.cancelled",
            run_id=run_id,
            turn=turn,
            duration_ms=round((time.time() - loop_start_time) * 1000),
        )
        raise

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        run_state.mark_failed(error_msg)
        run_state.append_event(EventType.RUN_FAILED, {"error": error_msg})
        await db_service.update_run_status(run_id, "failed", total_turns=turn, error=error_msg)

        logger.error(
            "agent_loop.failed",
            run_id=run_id,
            turn=turn,
            error=error_msg,
            traceback=traceback.format_exc(),
            duration_ms=round((time.time() - loop_start_time) * 1000),
        )


# ============================================================
# Gateway 驱动的 LLM 调用
# ============================================================

async def _call_model_via_gateway(
    gateway_router: GatewayExecutionPort,
    logical_model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    run_state: RunState,
    turn: int,
    stream: bool = True,
) -> tuple[list[str], list[ToolCallChunk], str, int, int, int | None]:
    """通过进程内 Gateway 执行一次 Agent Turn。

    GatewayModelRouter 已负责能力路由、熔断和安全 fallback，Agent Loop 不再做
    上游重试，避免流式首输出后重放造成重复文本。
    """
    model_key = logical_model
    try:
        text_parts: list[str] = []
        tool_calls: list[ToolCallChunk] = []
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0
        call_start = time.time()
        first_token_time: float | None = None
        call = GatewayModelCall(messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens, response_format=response_format)

        if stream:
            async for event in gateway_router.stream(logical_model, call):
                if isinstance(event, GatewayTextDelta):
                    if first_token_time is None:
                        first_token_time = time.time()
                    text_parts.append(event.content)
                    run_state.append_event(EventType.TEXT_DELTA, {"content": event.content, "turn": turn})
                elif isinstance(event, GatewayToolCallEvent):
                    if first_token_time is None:
                        first_token_time = time.time()
                    tool_calls.append(ToolCallChunk(id=event.tool_call.id, name=event.tool_call.name, arguments=event.tool_call.arguments))
                elif isinstance(event, GatewayCompletedEvent):
                    finish_reason = event.finish_reason
                    input_tokens = event.usage.input_tokens
                    output_tokens = event.usage.output_tokens
        else:
            result = await gateway_router.complete(logical_model, call)
            if result.content:
                text_parts.append(result.content)
                run_state.append_event(EventType.TEXT_DELTA, {"content": result.content, "turn": turn})
            tool_calls = [ToolCallChunk(id=tool.id, name=tool.name, arguments=tool.arguments) for tool in result.tool_calls]
            finish_reason = result.finish_reason
            input_tokens = result.usage.input_tokens
            output_tokens = result.usage.output_tokens

        call_duration = time.time() - call_start
        ttft_ms = round((first_token_time - call_start) * 1000) if first_token_time else None
        logger.debug("agent_loop.llm_call_success", run_id=run_state.run_id, turn=turn, stream=stream, duration_ms=round(call_duration * 1000), ttft_ms=ttft_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return text_parts, tool_calls, finish_reason, input_tokens, output_tokens, ttft_ms
    except RateLimitExceeded:
        raise
    except Exception as exc:
        logger.error("agent_loop.gateway_call_failed", run_id=run_state.run_id, turn=turn, model=logical_model, error_type=type(exc).__name__)
        raise

# ============================================================
# 带重试的工具执行
# ============================================================

async def _execute_tool_with_retry(
    executor: ToolExecutor,
    name: str,
    arguments: dict[str, Any],
    max_retries: int = 2,
) -> str:
    """
    带重试的工具执行（工具可能也有临时性故障）

    工具重试次数较少（2次），因为工具执行可能有副作用
    """
    attempt = 0
    last_exception = None

    while attempt < max_retries:
        attempt += 1
        try:
            result = await executor(name, arguments)
            return result
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            last_exception = e
            logger.warning(
                "agent_loop.tool_retry",
                tool_name=name,
                attempt=attempt,
                max_attempts=max_retries,
                error_type=type(e).__name__,
                error_msg=str(e),
            )
            if attempt < max_retries:
                await asyncio.sleep(0.5 * attempt)
            else:
                raise
        except Exception:
            # 非网络错误不重试（可能是业务错误）
            raise

    raise last_exception or RuntimeError(f"Tool {name} failed with unknown error")
