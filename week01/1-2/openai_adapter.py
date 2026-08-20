import os

from openai import OpenAI
from dotenv import load_dotenv
from model_adapter import ModelAdapter, ModelCapabilities, ModelRequest, ModelResult
import json
from typing import Any
from dataclasses import asdict

load_dotenv()
api_key = os.getenv("LLM_API_KEY")

class OpenAIAdapter(ModelAdapter):
    name = "openai"
    capabilities = ModelCapabilities(
        chat_completions=True,
        responses=True,
        structured_output="native_schema",
        tool_calling=True,
        supports_temperature=False,  # 仅 non-thinking 生效
        supports_top_p=True,  # 仅 non-thinking 生效
    )

    def __init__(self, apikey: str, base_url: str = ""):
        self.client = OpenAI(
            api_key=apikey,
            base_url=base_url if base_url else "http://ai-service.tal.com/openai-compatible/v1",
            timeout=30.0,
            max_retries=0,
        )

    def generate(self, request: ModelRequest) -> ModelResult:
        system = request.system
        kwargs: dict[str, Any] = {}
        if request.output_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "agent_result",
                    "strict": True,
                    "schema": request.output_schema,
                }
            }

        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        raw = self.client.responses.create(
            model="gpt-5.6-luna",
            instructions=system,
            input=[{"role": "user", "content": request.user}],
            # extra_body={"thinking": {"type": "disabled"}},
            max_output_tokens=request.max_output_token,
            **kwargs,
        )

        # choice = raw.choices[0]
        text = raw.output_text or ""
        usage = raw.usage

        data = json.loads(text) if request.output_schema else None

        return ModelResult(
            kind="structured" if data is not None else "text",
            text=text,
            data=data,
            finish_reason=raw.status,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            request_id=raw.id,
            tool_calls=[]
        )

if __name__ == '__main__':
    openai_adapter = OpenAIAdapter(
        apikey=api_key
    )
    req = ModelRequest(
        system="you are a helpful assistant",
        user="番茄炒蛋如何制作？",
        # temperature=0.7,
        output_schema={
            "type": "object",
            "properties": {
                "steps": {"type": "array", "items": {"type": "string"}},
                "ingredients": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["steps", "ingredients"],
            "additionalProperties": False
        }
    )
    result = openai_adapter.generate(req)
    # 如果不走pydantic，需要倒入asdict
    # print(f"result: {json.dumps(asdict(result), indent=2, ensure_ascii=False)}")
    # 如果使用pydantic，可以直接用model_dump_json()
    print(f"result: {result.model_dump_json(indent=2, ensure_ascii=False)}")