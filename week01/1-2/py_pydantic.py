from pydantic import BaseModel, Field
from dotenv import load_dotenv
from openai import OpenAI
from typing import Literal
import os
import json

class Result(BaseModel):
    step: str
    description: str

class AgentAction(BaseModel):
    # 概念：先用 Pydantic 定义“期望输出结构”，把输出格式从“自然语言约定”
    # 升级成“代码里的显式契约”。这样既方便人阅读，也方便程序做自动校验。
    step: Literal["inspect_logs", "run_tests", "read_code", "ask_user"] = Field(
        description="Agent 下一步要执行的动作"
    )
    reason: str = Field(description="选择这个动作的原因")
    needs_user_input: bool = Field(description="是否需要向用户补充提问")
    confidence: float = Field(ge=0, le=1, description="当前判断的置信度，范围 0 到 1")

load_dotenv()
apikey = os.getenv("TALAI_API_KEY")

client = OpenAI(
    base_url="http://ai-service.tal.com/openai-compatible/v1",
    api_key=apikey,
    timeout=30.0,
    max_retries=0
)

schema = Result.model_json_schema()

result = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[
        {"role": "system", "content": (
                "你是 Agent 决策器。必须输出 json。"
                "输出必须符合以下 JSON Schema：\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )},
        {"role": "user", "content": "番茄炒蛋如何制作？给我详细的步骤"}
    ],
    response_format={"type": "json_object"},
)

raw_text = result.choices[0].message.content

# Pydantic 的价值：JSON mode 只能尽量保证“像 JSON”，但不能保证字段名、枚举值、
# 类型和数值范围都完全符合业务要求；这里再做一次模型校验，才能把输出真正变成
# “可直接进入业务逻辑”的结构化数据。
res = Result.model_validate_json(raw_text)
print(f"res: {res}")