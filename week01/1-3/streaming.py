"""
SSE 流式聊天服务器（支持断线重连）
===================================
基于 FastAPI 实现的 Server-Sent Events (SSE) 流式聊天接口。
核心特性：客户端断开后，LLM 生成不中断，chunk 缓存在内存中；
客户端可通过 resume 接口从断点处继续接收。

运行方式:
    python streaming.py
    # 或者
    uvicorn streaming:app --host 0.0.0.0 --port 8000 --reload

接口:
    POST /v1/chat/stream           - 发起流式聊天（SSE），返回 request_id
    GET  /v1/chat/resume/{id}      - 断线重连，从指定 index 继续接收
    GET  /health                   - 健康检查
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field

# ============================================================
# 配置加载
# ============================================================

# 加载 .env 文件（项目根目录），读取 TALAI_API_KEY
_env_path = Path(__file__).resolve().parents[2] / ".env"  # week01/1-3 -> ai-agent/.env
load_dotenv(dotenv_path=_env_path)

# 复用 1-2/ 下的 adapter 体系（共享 base_url、模型配置等）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "1-2"))
from ds_adapter import DeepSeekAdapter  # noqa: E402

API_KEY = os.getenv("TALAI_API_KEY", "")
MODEL_NAME = "deepseek-v4-pro"

if not API_KEY:
    print("⚠️  警告: 未找到 TALAI_API_KEY 环境变量，请检查 .env 文件")

# ============================================================
# 通过 Adapter 复用配置，创建异步客户端
# ============================================================

_adapter = DeepSeekAdapter(api_key=API_KEY)
client = AsyncOpenAI(
    api_key=API_KEY,
    base_url=str(_adapter.client.base_url),  # 从 adapter 拿，不硬编码
)

# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(
    title="SSE Streaming Chat Server (断线重连版)",
    description="基于 FastAPI 的 LLM 流式聊天服务，支持断线重连续传",
    version="2.0.0",
)

# CORS 中间件 —— 允许所有来源（开发阶段方便前端调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 内存缓冲区（核心数据结构）
# ============================================================

# 每个生成任务的缓冲区结构
# key: request_id (str)
# value: {
#     "chunks": list[dict],      # 按序存储的 chunk 列表
#     "done": bool,              # 生成是否已完成
#     "done_time": float | None, # 完成时间戳（用于过期清理）
#     "task": asyncio.Task,      # 后台生成任务的引用
#     "event": asyncio.Event,    # 有新 chunk 时通知等待的消费者
# }
_buffers: dict[str, dict] = {}

# 缓冲区过期时间（秒）：完成后保留 5 分钟
BUFFER_TTL_SECONDS = 5 * 60


# ============================================================
# 请求/响应模型 (Pydantic)
# ============================================================

class ChatMessage(BaseModel):
    """单条聊天消息"""
    role: str = Field(..., description="角色: user / assistant / system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    """聊天请求体"""
    messages: list[ChatMessage] = Field(..., description="对话历史消息列表")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="生成温度")


# ============================================================
# 后台生成任务
# ============================================================

async def _background_generate(request_id: str, chat_request: ChatRequest) -> None:
    """
    后台异步任务：调用 LLM 流式 API，将 chunk 存入缓冲区。
    即使客户端断开，此任务也会继续运行直到生成完毕。
    """
    buf = _buffers[request_id]

    try:
        # 调用 OpenAI 兼容接口，开启流式模式
        stream = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[msg.model_dump() for msg in chat_request.messages],
            temperature=chat_request.temperature,
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
        )

        # 逐 chunk 读取并存入缓冲区
        async for chunk in stream:
            delta_content = chunk.choices[0].delta.content if chunk.choices else None

            if delta_content:
                event_data = {
                    "type": "delta",
                    "content": delta_content,
                    "request_id": request_id,
                }
                buf["chunks"].append(event_data)
                # 通知所有等待中的消费者：有新数据了
                buf["event"].set()
                buf["event"].clear()

        # 生成完毕，追加 done 事件
        buf["chunks"].append({
            "type": "done",
            "content": "",
            "request_id": request_id,
        })

    except OpenAIError as e:
        # LLM 调用失败，写入错误 chunk
        buf["chunks"].append({
            "type": "error",
            "content": f"LLM 调用失败: {str(e)}",
            "request_id": request_id,
        })

    except Exception as e:
        # 兜底异常
        buf["chunks"].append({
            "type": "error",
            "content": f"服务器内部错误: {str(e)}",
            "request_id": request_id,
        })

    finally:
        # 标记完成
        buf["done"] = True
        buf["done_time"] = time.time()
        # 最后再通知一次，唤醒等待的消费者
        buf["event"].set()


# ============================================================
# SSE 流式消费器（从缓冲区读取并推送）
# ============================================================

async def _stream_from_buffer(
    request: Request,
    request_id: str,
    start_index: int = 0,
) -> AsyncGenerator[str, None]:
    """
    从缓冲区读取 chunk 并格式化为 SSE 事件流。
    如果已有的 chunk 已经读完且生成未完成，则 await 等待新 chunk。

    Args:
        request: FastAPI Request 对象，用于检测断连
        request_id: 生成任务 ID
        start_index: 从哪个 index 开始发送（用于断线重连）
    """
    buf = _buffers.get(request_id)
    if buf is None:
        # 无效的 request_id
        error_event = json.dumps(
            {"type": "error", "content": "无效的 request_id，可能已过期", "request_id": request_id},
            ensure_ascii=False,
        )
        yield f"id: 0\ndata: {error_event}\n\n"
        return

    current_index = start_index

    while True:
        # 检测客户端是否断开
        if await request.is_disconnected():
            return

        # 读取所有已有但尚未发送的 chunk
        chunks = buf["chunks"]
        while current_index < len(chunks):
            chunk_data = chunks[current_index]
            event_json = json.dumps(chunk_data, ensure_ascii=False)
            # SSE id 字段：客户端可用 Last-Event-ID 重连
            yield f"id: {current_index}\ndata: {event_json}\n\n"
            current_index += 1

            # 如果是 done 或 error，流结束
            if chunk_data["type"] in ("done", "error"):
                return

        # 如果生成已完成，不再等待
        if buf["done"]:
            return

        # 等待新 chunk 到来（带超时，定期检查断连）
        try:
            await asyncio.wait_for(buf["event"].wait(), timeout=1.0)
        except asyncio.TimeoutError:
            # 超时后重新循环，检查断连状态
            continue


# ============================================================
# 缓冲区清理（后台定时任务）
# ============================================================

async def _cleanup_expired_buffers() -> None:
    """
    后台协程：每 30 秒检查一次，清理已完成且超过 TTL 的缓冲区。
    """
    while True:
        await asyncio.sleep(30)
        now = time.time()
        expired_ids = [
            rid for rid, buf in _buffers.items()
            if buf["done"] and buf["done_time"] is not None
            and (now - buf["done_time"]) > BUFFER_TTL_SECONDS
        ]
        for rid in expired_ids:
            del _buffers[rid]
        if expired_ids:
            print(f"🧹 清理了 {len(expired_ids)} 个过期缓冲区: {expired_ids}")


@app.on_event("startup")
async def _on_startup() -> None:
    """应用启动时，启动后台清理任务"""
    asyncio.create_task(_cleanup_expired_buffers())


# ============================================================
# API 路由
# ============================================================

@app.post("/v1/chat/stream")
async def chat_stream(request: Request, chat_request: ChatRequest):
    """
    流式聊天接口 (SSE) —— 支持断线重连

    1. 服务端为每个请求分配 request_id
    2. 后台 Task 调用 LLM，chunk 存入内存缓冲区
    3. SSE 流从缓冲区读取并推送，第一个事件包含 request_id
    4. 客户端断开后，后台 Task 继续生成
    5. 客户端可通过 GET /v1/chat/resume/{request_id} 续传

    Request Body:
        {
            "messages": [{"role": "user", "content": "你好"}],
            "temperature": 0.7
        }

    Response:
        Content-Type: text/event-stream
        第一个事件: data: {"type": "start", "request_id": "xxx"}
        后续事件:   data: {"type": "delta", "content": "...", "request_id": "xxx"}
        结束事件:   data: {"type": "done", "content": "", "request_id": "xxx"}
    """
    # 分配唯一 request_id
    request_id = str(uuid.uuid4())

    # 初始化缓冲区
    _buffers[request_id] = {
        "chunks": [],
        "done": False,
        "done_time": None,
        "event": asyncio.Event(),
        "task": None,
    }

    # 启动后台生成任务
    task = asyncio.create_task(_background_generate(request_id, chat_request))
    _buffers[request_id]["task"] = task

    # 构建 SSE 流：先发 start 事件（告知 request_id），再从缓冲区流式推送
    async def _full_stream() -> AsyncGenerator[str, None]:
        # 第一个事件：告知前端 request_id，用于后续断线重连
        start_event = json.dumps(
            {"type": "start", "content": "", "request_id": request_id},
            ensure_ascii=False,
        )
        yield f"id: -1\ndata: {start_event}\n\n"

        # 从 index 0 开始流式推送
        async for sse_line in _stream_from_buffer(request, request_id, start_index=0):
            yield sse_line

    return StreamingResponse(
        _full_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/chat/resume/{request_id}")
async def chat_resume(
    request: Request,
    request_id: str,
    last_event_id: int = Query(default=0, description="上次收到的最后一个事件 ID（chunk index）"),
):
    """
    断线重连接口

    客户端断开后，通过此接口继续接收剩余的 chunk。
    last_event_id 对应 SSE 的 id 字段，从该 index + 1 开始重发。

    示例:
        GET /v1/chat/resume/abc-123?last_event_id=5
        → 从第 6 个 chunk 开始推送
    """
    if request_id not in _buffers:
        return StreamingResponse(
            _error_stream(request_id, "request_id 不存在或已过期"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # 从 last_event_id + 1 开始（last_event_id 是客户端最后收到的）
    start_index = last_event_id + 1

    return StreamingResponse(
        _stream_from_buffer(request, request_id, start_index=start_index),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _error_stream(request_id: str, message: str) -> AsyncGenerator[str, None]:
    """生成一个包含错误信息的 SSE 流"""
    error_event = json.dumps(
        {"type": "error", "content": message, "request_id": request_id},
        ensure_ascii=False,
    )
    yield f"id: 0\ndata: {error_event}\n\n"


@app.get("/health")
async def health_check():
    """
    健康检查接口

    返回服务状态、模型信息、当前活跃的缓冲区数量。
    """
    active_buffers = sum(1 for buf in _buffers.values() if not buf["done"])
    total_buffers = len(_buffers)
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "api_base": str(_adapter.client.base_url),
        "active_generations": active_buffers,
        "total_buffers": total_buffers,
    }


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        "streaming:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
