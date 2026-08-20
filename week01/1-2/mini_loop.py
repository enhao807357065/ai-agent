import os
from dotenv import load_dotenv
from model_adapter import ModelAdapter, ModelResult, ModelRequest
from py_pydantic import AgentAction
from ds_adapter import DeepSeekAdapter
from openai_adapter import OpenAIAdapter

class MiniAgentLoop:
    client: ModelAdapter
    api_key: str

    def __init__(self, client: ModelAdapter):
        self.client = client

    def run(self, input: str) -> AgentAction:
        request = ModelRequest(
            system=(
                "你是编码 Agent 的决策器。根据目标选择 inspect、edit、"
                "run_tests 或 finish。不要声称执行了尚未执行的动作。"
            ),
            user=input,
            max_output_tokens=1024,
            output_schema=AgentAction.model_json_schema(),
        )
        result = self.client.generate(request)
        if result.kind != "structured" or result.data is None:
            raise RuntimeError("模型没有返回结构化动作")

        # Adapter 负责协议归一化，Loop 仍负责领域对象校验。
        return AgentAction.model_validate(result.data)

import os

provider = os.getenv("MODEL_PROVIDER", "deepseek")
print(f"provider: {provider}")

if provider == "deepseek":
    adapter: ModelAdapter = DeepSeekAdapter(
        api_key=os.environ["LLM_API_KEY"]
    )
elif provider == "openai":
    adapter = OpenAIAdapter(
        apikey=os.environ["LLM_API_KEY"],
    )
else:
    raise ValueError(f"未知 MODEL_PROVIDER: {provider}")

loop = MiniAgentLoop(adapter)
action = loop.run("检查 tests/test_api.py 失败原因，当前尚未运行测试。")
print(action)