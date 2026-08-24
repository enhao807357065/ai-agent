"""
Agent Loop — 核心执行引擎（增强版）

增强特性：
    1. 异常重试：LLM 调用失败时自动重试（指数退避）
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
from typing import Any, Callable, Awaitable

import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryCallState,
)

from app.core.config import settings
from app.models.events import EventType
from app.models.streaming import StreamingModel, TextChunk, ToolCallChunk, StreamDone
from app.services.run_store import RunState
from app.services.db_service import db_service
from app.services.rate_limiter import rate_limiter, RateLimitExceeded

logger = structlog.get_logger(__name__)

# 工具执行器类型：接收 (tool_name, arguments) -> 返回结果字符串
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


# ============================================================
# 重试配置
# ============================================================

# 可重试的异常类型（网络错误、超时、服务端 5xx）
RETRIABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)

try:
    from openai import APITimeoutError, APIConnectionError, InternalServerError, RateLimitError
    RETRIABLE_EXCEPTIONS = RETRIABLE_EXCEPTIONS + (
        APITimeoutError,
        APIConnectionError,
        InternalServerError,
        RateLimitError,
    )
except ImportError:
    pass

try:
    from anthropic import APITimeoutError as AnthropicTimeout
    from anthropic import APIConnectionError as AnthropicConnError
    from anthropic import InternalServerError as AnthropicServerError
    from anthropic import RateLimitError as AnthropicRateLimit
    RETRIABLE_EXCEPTIONS = RETRIABLE_EXCEPTIONS + (
        AnthropicTimeout,
        AnthropicConnError,
        AnthropicServerError,
        AnthropicRateLimit,
    )
except ImportError:
    pass


def _log_retry(retry_state: RetryCallState) -> None:
    """重试前记录日志"""
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "agent_loop.llm_retry",
        attempt=retry_state.attempt_number,
        max_attempts=settings.LLM_MAX_RETRIES,
        exception_type=type(exception).__name__ if exception else None,
        exception_msg=str(exception) if exception else None,
        wait_seconds=retry_state.next_action.sleep if retry_state.next_action else 0,
    )


# ============================================================
# Agent Loop 主函数
# ============================================================

async def agent_loop(
    run_state: RunState,
    model: StreamingModel,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_executor: ToolExecutor | None = None,
    temperature: float = 0.7,
    max_turns: int = 10,
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
        model=model.model_name,
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

            # ---- 调用模型（带重试）----
            text_parts: list[str] = []
            tool_calls: list[ToolCallChunk] = []
            finish_reason = "stop"
            input_tokens = 0
            output_tokens = 0

            try:
                text_parts, tool_calls, finish_reason, input_tokens, output_tokens, ttft_ms = (
                    await _call_model_with_retry(
                        model=model,
                        messages=messages,
                        tools=tools,
                        temperature=temperature,
                        run_state=run_state,
                        turn=turn,
                        stream=stream,
                    )
                )
            except Exception as e:
                # 所有重试耗尽后仍然失败
                error_msg = f"LLM call failed after {settings.LLM_MAX_RETRIES} retries: {type(e).__name__}: {e}"
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
# 带重试的 LLM 调用
# ============================================================

async def _call_model_with_retry(
    model: StreamingModel,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    run_state: RunState,
    turn: int,
    stream: bool = True,
) -> tuple[list[str], list[ToolCallChunk], str, int, int, int | None]:
    """
    带重试机制的模型调用

    Args:
        stream: True 使用流式（逐 chunk 推事件），False 使用非流式（一次性返回）

    Returns:
        (text_parts, tool_calls, finish_reason, input_tokens, output_tokens, ttft_ms)
        ttft_ms: 首 token 延迟（毫秒），仅流式模式有值，非流式为 None
    """
    attempt = 0
    last_exception = None
    model_key = model.model_name

    while attempt < settings.LLM_MAX_RETRIES:
        attempt += 1
        try:
            # ---- 限流：请求前获取 RPM 配额 ----
            await rate_limiter.acquire_request(model_key)

            text_parts: list[str] = []
            tool_calls: list[ToolCallChunk] = []
            finish_reason = "stop"
            input_tokens = 0
            output_tokens = 0

            call_start = time.time()
            first_token_time: float | None = None

            if stream:
                # ---- 流式调用 ----
                async for chunk in model.stream(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                ):
                    if isinstance(chunk, TextChunk):
                        if first_token_time is None:
                            first_token_time = time.time()
                        text_parts.append(chunk.content)
                        run_state.append_event(EventType.TEXT_DELTA, {
                            "content": chunk.content,
                            "turn": turn,
                        })
                    elif isinstance(chunk, ToolCallChunk):
                        if first_token_time is None:
                            first_token_time = time.time()
                        tool_calls.append(chunk)
                    elif isinstance(chunk, StreamDone):
                        finish_reason = chunk.finish_reason
                        input_tokens = chunk.input_tokens
                        output_tokens = chunk.output_tokens
            else:
                # ---- 非流式调用 ----
                from app.models.streaming import CompletionResult
                result: CompletionResult = await model.complete(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                )
                if result.content:
                    text_parts.append(result.content)
                    # 非流式也发一个 text_delta 事件（完整内容一次性推送）
                    run_state.append_event(EventType.TEXT_DELTA, {
                        "content": result.content,
                        "turn": turn,
                    })
                tool_calls = result.tool_calls
                finish_reason = result.finish_reason
                input_tokens = result.input_tokens
                output_tokens = result.output_tokens

            call_duration = time.time() - call_start
            ttft_ms = round((first_token_time - call_start) * 1000) if first_token_time else None

            logger.debug(
                "agent_loop.llm_call_success",
                run_id=run_state.run_id,
                turn=turn,
                attempt=attempt,
                stream=stream,
                duration_ms=round(call_duration * 1000),
                ttft_ms=ttft_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            # ---- 限流：请求后上报 token 用量（TPM）----
            total_tokens = input_tokens + output_tokens
            if total_tokens > 0:
                await rate_limiter.report_tokens(model_key, total_tokens)

            return text_parts, tool_calls, finish_reason, input_tokens, output_tokens, ttft_ms

        except RETRIABLE_EXCEPTIONS as e:
            last_exception = e
            wait_time = settings.LLM_RETRY_DELAY * (2 ** (attempt - 1))  # 指数退避

            logger.warning(
                "agent_loop.llm_retry",
                run_id=run_state.run_id,
                turn=turn,
                attempt=attempt,
                max_attempts=settings.LLM_MAX_RETRIES,
                error_type=type(e).__name__,
                error_msg=str(e),
                wait_seconds=wait_time,
            )

            if attempt < settings.LLM_MAX_RETRIES:
                await asyncio.sleep(wait_time)
            else:
                raise

        except Exception as e:
            # 不可重试的异常直接抛出
            logger.error(
                "agent_loop.llm_call_non_retriable",
                run_id=run_state.run_id,
                turn=turn,
                attempt=attempt,
                error_type=type(e).__name__,
                error_msg=str(e),
            )
            raise

    # 理论上不会到这里，但防御性编程
    raise last_exception or RuntimeError("LLM call failed with unknown error")


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
