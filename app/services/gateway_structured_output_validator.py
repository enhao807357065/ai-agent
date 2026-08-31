"""Gateway 对 json_schema 输出提供的本地交付校验。"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from app.models.gateway import GatewayModelCall


class GatewayStructuredOutputSchemaError(Exception):
    """调用方提供的 json_schema 本身无效；请求不应送往上游。"""


class GatewayStructuredOutputInvalid(Exception):
    """上游返回文本不是合法 JSON 或不满足调用方提供的 schema。"""


def json_schema_from_call(call: GatewayModelCall) -> dict[str, Any] | None:
    """返回严格 JSON Schema；非 json_schema 请求返回 None。"""
    response_format = call.response_format or {}
    if response_format.get("type") != "json_schema":
        return None

    definition = response_format.get("json_schema")
    if not isinstance(definition, dict):
        raise GatewayStructuredOutputSchemaError("response_format.json_schema must be an object")
    schema = definition.get("schema")
    if not isinstance(schema, dict):
        raise GatewayStructuredOutputSchemaError("response_format.json_schema.schema must be an object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise GatewayStructuredOutputSchemaError("response_format.json_schema.schema is invalid") from exc
    return schema


def validate_structured_output(content: str, call: GatewayModelCall) -> None:
    """校验结构化输出的完整文本，但不改变对外 ``content`` 格式。

    ``json_object`` 的交付契约是「合法 JSON，且根节点为 object」；
    ``json_schema`` 则由调用方给出的 JSON Schema 决定根节点和字段约束。
    """
    response_format = call.response_format or {}
    response_type = response_format.get("type")
    if response_type not in {"json_object", "json_schema"}:
        return

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GatewayStructuredOutputInvalid("Upstream output is not valid JSON.") from exc

    if response_type == "json_object":
        if not isinstance(payload, dict):
            raise GatewayStructuredOutputInvalid("Upstream JSON output must have an object root.")
        return

    schema = json_schema_from_call(call)
    assert schema is not None  # 已由 response_type == "json_schema" 保证。
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        raise GatewayStructuredOutputInvalid("Upstream output does not satisfy the requested JSON Schema.") from exc
