import os

from openai import OpenAI
from dotenv import load_dotenv
from model_adapter import ModelAdapter, ModelCapabilities, ModelRequest, ModelResult
import json
from typing import Any
from dataclasses import asdict

load_dotenv()
api_key = os.getenv("TALAI_API_KEY")

class DeepSeekAdapter(ModelAdapter):
    name = "deepseek"
    capabilities = ModelCapabilities(
        chat_completions=True,
        responses=False,
        structured_output="json_mode",
        tool_calling=True,
        supports_temperature=True,  # 仅 non-thinking 生效
        supports_top_p=True,  # 仅 non-thinking 生效
    )

    def __init__(self, api_key: str) -> None:
        self.client = OpenAI(
            api_key=api_key,
            base_url="http://ai-service.tal.com/openai-compatible/v1",
            timeout=30.0,
            max_retries=0,
        )

    def generate(self, request: ModelRequest) -> ModelResult:
        system = request.system
        kwargs: dict[str, Any] = {}
        if request.output_schema:
            system += (
                    "\n必须输出 json，并符合此 JSON Schema：\n"
                    + json.dumps(request.output_schema, ensure_ascii=False)
            )
            kwargs["response_format"] = {"type": "json_object"}

        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        raw = self.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_output_tokens,
            extra_body={"thinking": {"type": "disabled"}},
            **kwargs,
        )

        choice = raw.choices[0]
        text = choice.message.content or ""
        usage = raw.usage

        # 可能会炸，做一下try catch
        data = json.loads(text) if request.output_schema else None

        return ModelResult(
            kind = "structured" if data is not None else "text",
            text = text,
            data = data,
            finish_reason = choice.finish_reason,
            input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0,
            request_id = raw.id,
            tool_calls=[]
        )

if __name__ == '__main__':
    ds = DeepSeekAdapter(api_key=api_key)
    req = ModelRequest(
        system="you are a helpful assistant",
        user="番茄炒蛋如何制作？",
        temperature=0.7,
        output_schema="json_mode"
    )
    result = ds.generate(req)
    # 如果不走pydantic，需要倒入asdict
    # print(f"result: {json.dumps(asdict(result), indent=2, ensure_ascii=False)}")
    # 如果使用pydantic，可以直接用model_dump_json()
    print(f"result: {result.model_dump_json(indent=2, ensure_ascii=False)}")