# smolagents + AI Gateway（本地开发示例）
#
# 前置条件：Gateway 已启动并兼容 OpenAI Chat Completions。
# 本项目当前已探测到：GET http://127.0.0.1:8000/v1/models
# 返回 chat-default / chat-fast / reasoning-pro。
#
# 不要把真实 API Key 写入此文件。运行前在 shell 中设置 GATEWAY_API_KEY。

from __future__ import annotations

import os

from smolagents import CodeAgent, OpenAIModel, tool


@tool
def multiply(left: int, right: int) -> int:
    """Multiply two integers.

    Args:
        left: First integer.
        right: Second integer.
    """
    return left * right


def main() -> None:
    api_key = os.environ.get("GATEWAY_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置 GATEWAY_API_KEY；不要把密钥写入源码。")

    # Gateway 必须暴露 OpenAI Chat Completions 兼容接口：POST /v1/chat/completions。
    model = OpenAIModel(
        model_id=os.environ.get("GATEWAY_MODEL_ID", "chat-default"),
        api_base=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=api_key,
        temperature=0,
    )

    # CodeAgent 会生成 Python 来组合工具和中间结果。
    # 本例没有额外开放 import，保持最小权限。
    agent = CodeAgent(
        tools=[multiply],
        model=model,
        max_steps=4,
        stream_outputs=True,
    )
    result = agent.run("计算 37 乘以 42，再用一句中文说明结果。")
    print(f"\n最终结果：{result}")


if __name__ == "__main__":
    main()
