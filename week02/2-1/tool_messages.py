import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from tool_definition import ToolError


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    arguments_json: str

class ToolResultMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False

    def to_model_message(self) -> dict[str, str]:
        return {
            "role": self.role,
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }

def success_message(call: ToolCall, output: BaseModel) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        name=call.name,
        content=output.model_dump_json(),
        is_error=False
    )

def error_message(call: ToolCall, error: ToolError) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        name=call.name,
        content=json.dumps(
            {"error": error.model_dump()},
            ensure_ascii=False,
        ),
        is_error=True,
    )