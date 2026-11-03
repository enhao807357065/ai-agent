# smolagents + AI Gateway：结构化 Tool Calling 示例
#
# 与 gateway_code_agent.py 的差别：
# ToolCallingAgent 要求模型/Gateway 支持 OpenAI tools/function-calling 语义。
# 它不执行 LLM 生成的 Python，而是接收结构化工具调用。

from __future__ import annotations

import os

from smolagents import OpenAIModel, ToolCallingAgent, tool


@tool
def lookup_course(course_id: str) -> str:
    """Look up one course by its identifier.

    Args:
        course_id: Course identifier, such as PY-101.
    """
    catalog = {
        "PY-101": "Python 基础，12 节课",
        "AG-201": "Agent 工程，10 节课",
    }
    return catalog.get(course_id, "课程不存在")


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

    # Gateway 必须正确处理 request.tools、tool_choice、tool_calls 及 tool result 回合。
    agent = ToolCallingAgent(
        tools=[lookup_course],
        model=model,
        max_steps=3,
        stream_outputs=True,
    )
    result = agent.run("查询 AG-201，并用中文告诉我课程名称和节数。")
    print(f"\n最终结果：{result}")


if __name__ == "__main__":
    main()
