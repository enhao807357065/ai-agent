import asyncio
import json
import os
import sys
import time
import re

from openai import OpenAI
from dotenv import load_dotenv

from sandbox_runner import run_python_in_sandbox

load_dotenv()

apikey = os.getenv("LLM_API_KEY")

client = OpenAI(
    base_url="http://ai-service.tal.com/openai-compatible/v1",
    api_key=apikey
)

SYSTEM_PROMPT = (
    "你是会使用工具的助手"
    '问天气时，若还没有结果，只输出JSON：{"type": "tool_call", "name": "get_weather", "arguments": {"city": "城市名"}}'
    '当用户要求执行一小段 Python 代码、做计算或验证结果时，若还没有结果，只输出JSON：{"type": "tool_call", "name": "run_python", "arguments": {"code": "Python代码"}}'
    "拿到工具结果后再自然语言回答"
)

async def get_weather(args: dict) -> dict:
    """一个假的天气工具，用来展示tool execution"""
    city = args.get("city", "北京")
    await asyncio.sleep(1)
    return {
        "content": [
            {
                "type": "text",
                "text": f"{city}当前天气晴，33度，微风"
            }
        ],
        "details": {
            "city": city,
            "weather": "晴",
            "temperature_c": 33,
        }
    }

async def run_python_tool(args: dict) -> dict:
    """
    把一小段python代码交给最小sandbox执行
    """
    code = args.get("code", "")
    result = await run_python_in_sandbox(code)
    stdout_text = result["stdout"]
    stderr_text = result["stderr"]
    if result["ok"]:
        output_text = (
            "Python 执行成功。\n"
            f"输出：{stdout_text or 'No Output.'}"
        )
    else:
        detail_text = stderr_text or "No stderr."
        output_text = (
            f"Python 执行失败。({result['error_type']})\n"
            f"说明：{result['error_message']}"
            f"错误详情：{detail_text}"
        )
    return {
        "content": [
            {
                "type": "text",
                "text": output_text
            }
        ],
        "details": result
    }

def render(message: dict) -> str:
    """把内部message结构渲染成可读文本"""
    parts = []
    for block in message["content"]:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "toolCall":
            parts.append(f"[toolCall] {block['name']}({json.dumps(block['arguments'], ensure_ascii=False)})")
    print(f"origin_message: {message}, render: {'\n'.join(parts)}")
    return "\n".join(parts)

def to_llm(messages: list[dict]) -> list[dict]:
    """把内部消息格式转成LLM输入格式。"""
    out = []
    for message in messages:
        if message["role"] in {"user", "assistant"}:
            out.append({"role": message["role"], "content": render(message)})
        elif message["role"] == "toolResult":
            out.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result for {message['tool_name']}："
                        f"{json.dumps(message['details'], ensure_ascii=False)}"
                    )
                }
            )
    return out


def parse_assistant(text: str) -> dict:
    """
    把模型输出解析成统一的assistant message

    如果模型输出的是{"type":"tool_call"...}，把这种转成内部的toolCall block
    否则就按文本处理
    """
    try:
        match = re.search(r"\{.*\}", text, re.S)
        payload = json.loads(match.group(0)) if match else {}
        if payload.get("type", "") == "tool_call":
            return {
                "role": "assistant",
                "content": [{
                    "type": "toolCall",
                    "id": "call_001",
                    "name": payload["name"],
                    "arguments": payload["arguments"]
                }]
            }
    except Exception as e:
        pass

    return {"role": "assistant", "content": [{"type": "text", "text": text}]}

def extract_response_text(response, on_text_delta=None) -> str:
    """
    兼容普通响应与stream=True的分片响应，并支持边收边消费文本分片
    """
    if hasattr(response, "choices"):
        text = response.choices[0].message.content or ""
        if text and on_text_delta:
            on_text_delta(text)
        return text

    chunks: list[str] = []
    for chunk in response:
        if not getattr(chunk, "choices", None):
            continue

        delta = chunk.choices[0].delta
        if delta and delta.content:
            chunks.append(delta.content)
            if on_text_delta:
                on_text_delta(delta.content)

    return "".join(chunks)

def stream_to_terminal(text: str):
    """把模型分片即时写到终端"""
    sys.stdout.write(text)
    sys.stdout.flush()

async def llm_complete(messages: list[dict], on_text_delta=None) -> str:
    """调用模型"""
    def _call() -> str:
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *to_llm(messages)],
            stream=False,
            reasoning_effort="low",
            extra_body={"thinking": {"type": "disabled"}}
        )
        return extract_response_text(response, on_text_delta)

    return await asyncio.to_thread(_call)

async def run_tool_call(call: dict, tools: dict) -> dict:
    """统一执行工具：方便后续接入sandbox和其他runtime"""
    tool = tools[call["name"]]
    return await tool["handler"](call["arguments"])

async def agent_loop(user_text: str, debug: bool = True) -> dict:
    """
    一个最小可执行agent loop

    主流程：
    1. 用户消息进入上下文
    2. 调模型生成assistant message
    3. 如果assistant里有tool_call，就执行工具
    4. 把 tool result 回填到上下文
    5. 再调下一轮模型，直到assistant不再请求工具
    """
    start = time.perf_counter()
    executed_calls = []
    tool_results = []

    ctx = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "tools": {
            "get_weather": {
                "handler": get_weather,
                "sandboxed": False
            },
            "run_python": {
                "handler": run_python_tool,
                "sandboxed": True
            }
        }
    }

    if debug:
        print("[agent_start]")
        print("[turn_start]")

    while True:
        on_text_delta = stream_to_terminal if debug else None
        if debug:
            print("[assistant_stream]", end=" ", flush=True)
        raw_text = await llm_complete(ctx["messages"], on_text_delta)
        if debug:
            print()
        assistant = parse_assistant(raw_text)
        ctx["messages"].append(assistant)
        calls = [block for block in assistant["content"] if block["type"] == "toolCall"]
        if debug:
            if calls:
                print("[message_end]", render(assistant))
            else:
                print("[message_end] assistant streamed")

        if not calls:
            # 没有工具调用，直接退出
            break

        # 执行工具
        for call in calls:
            tool_meta = ctx["tools"][call["name"]]
            executed_calls.append({
                "id": call["id"],
                "name": call["name"],
                "arguments": call["arguments"],
                "sandboxed": tool_meta["sandboxed"]
            })
            if debug:
                print("[tool_start]", call["name"], call["arguments"])
            result = await run_tool_call(call, ctx["tools"])
            tool_results.append({
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "details": result["details"]
            })
            if debug:
                print("[tool_end]", result["details"])

            # 把tool result回填到上下文
            tool_result = {
                "role": "toolResult",
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "content": result["content"],
                "details": result["details"],
                "is_error": not result["details"].get("ok", True)
            }
            ctx["messages"].append(tool_result)
            if debug:
                print("[message_end]", render(tool_result))

        if debug:
            print("[turn_end]")
            print("[turn_start]")

    final_answer = render(ctx["messages"][-1])
    duration_ms = int((time.perf_counter()-start)*1000)
    result = {
        "final_answer": final_answer,
        "messages": ctx["messages"],
        "tool_calls": executed_calls,
        "tool_results": tool_results,
        "duration_ms": duration_ms
    }

    if debug:
        print("[turn_end]")
        print("[agent_end]")
        print("\nfinal answer:")
        print(final_answer)

    return result

if __name__ == '__main__':
    asyncio.run(agent_loop("给我写段python代码来计算100以内所有整数之和？"))