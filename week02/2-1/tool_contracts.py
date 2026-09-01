"""工具 Runtime 跨模块共享的数据契约。

这里定义“工具能接收什么、成功返回什么、失败如何表达”；
不放调用身份（ExecutionContext），也不放 LLM Provider 的消息格式。
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ToolInput(BaseModel):
    """所有工具入参的基类：拒绝 LLM 传入的未知字段。"""

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """所有工具成功输出的基类：仅暴露显式声明的字段。"""

    model_config = ConfigDict(extra="forbid")


class ToolErrorCode(StrEnum):
    # 调用方 / 输入问题：通常不可重试
    INVALID_ARGUMENT = "invalid_argument"
    VALIDATION_ERROR = "validation_error"

    # 身份与权限问题：通常不可重试
    UNAUTHENTICATED = "unauthenticated"
    PERMISSION_DENIED = "permission_denied"

    # 业务状态问题：通常不可重试
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PRECONDITION_FAILED = "precondition_failed"

    # 资源和系统问题：可能可重试
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    UPSTREAM_ERROR = "upstream_error"
    INTERNAL_ERROR = "internal_error"
    APPROVAL_REQUIRED = "approval_required"
    INVALID_OUTPUT = "invalid_output"
    ORDER_STATUS_CANNOT_CANCEL = "can_not_cancel"


class ToolError(BaseModel):
    """可安全序列化给 LLM 或 API 调用方的统一工具错误。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: ToolErrorCode
    message: str
    retryable: bool = False
    trace_id: str | None = None

class ToolExecutionError(Exception):
    """Handler 主动声明的、可安全返回给模型的工具执行错误。"""

    def __init__(
        self,
        *,
        code: ToolErrorCode,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable