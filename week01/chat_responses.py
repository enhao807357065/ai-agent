from openai import OpenAI
from dotenv import load_dotenv
import os
import json
from typing import Any

load_dotenv()

apikey = os.getenv("LLM_API_KEY")

client = OpenAI(
    base_url="http://ai-service.tal.com/openai-compatible/v1",
    api_key=apikey
)

# chat
# response = client.chat.completions.create(
#     model="deepseek-v4-pro",
#     temperature=0.7,
#     messages=[
#         {"role": "system", "content": "you are a helpful assistant"},
#         {"role": "user", "content": "你好"}
#     ]
# )
# print(f"response: {response.model_dump_json(ensure_ascii=False)}")

# responses
response_v1 = client.responses.create(
    model="gpt-5.6-luna",
    # temperature=0.7,
    instructions="you are a helpful assistant",
    input=[
        # {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "你好，我的名字是张飞"}
    ]
)
print(f"response_v1: {response_v1.model_dump_json(ensure_ascii=False)}")
print(f"response_v1: {response_v1.output_text}")

# print(f"previous_id: {response_v1.previous_response_id}, id: {response_v1.id}")

# response_v2 = client.responses.create(
#     model="gpt-5.6-luna",
#     # temperature=0.7,
#     instructions="you are a helpful assistant",
#     # previous_response_id=response_v1.id,      # tal网关不支持previous_response_id
#     input=[
#         # {"role": "system", "content": "you are a helpful assistant"},
#         {"role": "user", "content": "我是谁？"}
#     ]
# )
# print(f"reponse_v2: {response_v2.output_text}")

def get_weather(city: str) -> str:
    return f"{city} 今天天气晴朗，气温28度"

tools = [
    {
        "type": "function",
        "name": "get_weather",
        "description": "根据城市信息查询天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名字"
                }
            },
            "required": ["city"]
        }
    }
]

chat_tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "根据城市信息查询天气",
            "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名字"
                }
            },
            "required": ["city"]
        }
        }
    }
]

ctx = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "北京今天天气怎么样？"}
    ]
# response_v3 = client.responses.create(
#     model="gpt-5.6-luna",
#     # temperature=0.7,
#     input=ctx,
#     tools=tools
# )
# # print(f"response_v1: {json.dumps(response_v1.model_dump_json(ensure_ascii=False))}")
# print(f"response_v3: {response_v3.model_dump_json(ensure_ascii=False)}")

print("=========================== responses ===============================")
# 流式接收function_call
stream2 = client.responses.create(
    model="gpt-5.6-luna",
    # temperature=0.7,
    input=ctx,
    tools=tools,
    stream=True,
)
for chunk in stream2:
    print(f"chunk: {chunk}")

print("=========================== chat ===============================")
stream = client.chat.completions.create(
    model="gpt-5.6-luna",
    # temperature=0.7,
    messages=ctx,
    tools=chat_tools,
    stream=True,
)
tool_call_buffers: dict[int, dict] = {}
for chunk in stream:
    if not chunk.choices:
        continue

    print(f"chunk: {chunk}")
    choice = chunk.choices[0]
    delta = choice.delta

    if delta.tool_calls:
        for tc_delta in delta.tool_calls:
            idx = tc_delta.index
            if idx not in tool_call_buffers:
                tool_call_buffers[idx] = {
                    "id": tc_delta.id or "",
                    "name": "",
                    "arguments": "",
                }
            buf = tool_call_buffers[idx]
            if tc_delta.id:
                buf["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    buf["name"] = tc_delta.function.name
                if tc_delta.function.arguments:
                    buf["arguments"] += tc_delta.function.arguments
        print(f"buf: {buf}")

#
# if response_v3.output and response_v3.output[0].type == "function_call":
#     output = response_v3.output[0]
#     ctx.append(output)
#     if output.name == "get_weather":
#         params = json.loads(output.arguments)
#         result = get_weather(params["city"])
#     current = {
#         "type": "function_call_output",
#         "call_id": response_v3.output[0].call_id,
#         "output": result
#     }
#     ctx.append(current)
#     response_v4 = client.responses.create(
#         model="gpt-5.6-luna",
#         # temperature=0.7,
#         input=ctx,
#         tools=tools
#     )
#     # print(f"response_v1: {json.dumps(response_v1.model_dump_json(ensure_ascii=False))}")
#     print(f"response_v4: {response_v4.model_dump_json(ensure_ascii=False)}")


# stream = client.chat.completions.create(
#     model="gpt-5.6-luna",
#     # temperature=0.7,
#     messages=[
#         {"role": "system", "content": "you are a helpful assistant"},
#         {"role": "user", "content": "番茄炒蛋如何制作？"}
#     ],
#     stream=True,
# )
#
# print(f"stream type: {type(stream)}")
# for msg in stream:
#     print(f"{msg.model_dump_json()}")
#     if msg.choices[0].delta.content:
#         print(f"msg: {msg.choices[0].delta.content}")

messages = [
    {"role": "system", "content": "you are a helpful assistant"},
    {"role": "user", "content": "hi"}
]

kwargs: dict[str, Any] = {
            "model": "deepseek-v4-pro",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 4096,
            "stream": True,
        }

def func_print(**kwargs):
    print(kwargs)

func_print(**kwargs)