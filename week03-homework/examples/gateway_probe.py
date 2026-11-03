# smolagents + AI Gateway：最小 OpenAI Chat Completions 连通性探针
#
# 此脚本不运行 Agent，也不发送 tools。先确认 Gateway 能接受 smolagents 使用的
# OpenAIModel 基础请求形状。若此处失败，先排查 Gateway 的 Chat Completions 兼容性。

from __future__ import annotations

import os

from smolagents import OpenAIModel


def main() -> None:
    api_key = os.environ.get("GATEWAY_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置 GATEWAY_API_KEY；不要把密钥写入源码。")

    model = OpenAIModel(
        model_id=os.environ.get("GATEWAY_MODEL_ID", "chat-default"),
        api_base=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=api_key,
        temperature=0,
    )
    answer = model([
        {"role": "user", "content": "只回答：gateway-ok"},
    ])
    print(answer.content)


if __name__ == "__main__":
    main()
