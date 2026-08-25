import os
from openai import OpenAI
from dotenv import load_dotenv
from field_rule import AgentDecision

load_dotenv()

apikey = os.getenv("TALAI_API_KEY")

client = OpenAI(
    base_url="http://ai-service.tal.com/openai-compatible/v1",
    api_key=apikey
)

# 一步到位：调用 LLM + 解析成结构体
response = client.responses.parse(
    model="gpt-5.6-luna",
    input=[
        {
            "role": "system",
            "content": (
                "你是知识库 Agent 的决策器。"
                "资料不足时继续搜索，资料充分时结束并回答。"
            ),
        },
        {
            "role": "user",
            "content": "公司差旅报销需要哪些材料？",
        },
    ],
    text_format=AgentDecision,  # 传入 Pydantic 类
)

decision = response.output_parsed
print(f"decision: {decision}")