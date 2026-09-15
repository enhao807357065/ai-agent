from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


# 权限模式
class PermissionMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"                               # 计划，只能进行读操作
    BYPASS_PERMISSIONS = "bypassPermissions"    # 只跳过普通用户确认，不会跳过白名单、RBAC、业务检查或强制审批。
    DONT_ASK = "dontAsk"        # 当前通道不能互动确认，如定时任务、后台批处理，遇到需要确认的动作直接拒绝，而不是卡住


# 调用这个工具会造成什么影响
class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"


# 风险级别
class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# 权限3态
class DecisionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


Permission = Literal["order:read", "refund:create", "shell:run", "transfer:execute"]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    trace_id: str
    user_id: str
    tenant_id: str
    mode: PermissionMode
    permissions: frozenset[Permission]
    allowed_tools: frozenset[str]
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    effect: Effect
    risk: Risk
    permission: Permission
    requires_approval: bool
    timeout_seconds: float
    max_retries: int
    idempotent: bool


class StrictArgs(BaseModel):
    """模型只能提交 Schema 允许的业务候选参数。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class StrictOutput(BaseModel):
    """Handler 返回值必须经过白名单输出契约后才能进入模型上下文。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GetOrderArgs(StrictArgs):
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")


class CreateRefundArgs(StrictArgs):
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")
    amount: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=4, max_length=200)


class RunShellArgs(StrictArgs):
    command: str = Field(min_length=1, max_length=200)


# 转账参数模型
class TransferArgs(StrictArgs):
    from_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    to_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    amount: float = Field(gt=0, le=100000)


class GetOrderOutput(StrictOutput):
    status: str
    refundable: float
    customer_email: str


class CreateRefundOutput(StrictOutput):
    refund_id: str
    idempotency_key: str
    tenant_id: str
    order_id: str
    amount: float
    status: str


class RunShellOutput(StrictOutput):
    simulated: bool
    command: str
    stdout: str


class TransferOutput(StrictOutput):
    txn_id: str
    from_account: str = Field(validation_alias="from", serialization_alias="from")
    to_account: str = Field(validation_alias="to", serialization_alias="to")
    amount: float
    status: str


ArgsModel = StrictArgs
Handler = Callable[[str, ArgsModel, ExecutionContext], Awaitable[Mapping[str, Any]]]
Precheck = Callable[[ArgsModel, ExecutionContext], Awaitable[None]]
CanonicalTarget = Callable[[ArgsModel], str]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    parameters_model: type[StrictArgs]
    output_model: type[StrictOutput]
    policy: ToolPolicy
    handler: Handler
    canonical_target: CanonicalTarget
    precheck: Precheck | None = None

    def to_model_tool(self) -> dict[str, Any]:
        """只投影模型需要的描述和 JSON Schema，不暴露 handler 与治理策略。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    """一次 Run 固化的 name -> ToolDefinition 路由；模型永远看不到 version。"""

    routes: Mapping[str, ToolDefinition]
    revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))


class ToolRegistry:
    """控制面：管理版本、按路由生成 Snapshot、维护实时 family/version kill switch。"""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDefinition] = {}
        self._family_enabled: dict[str, bool] = {}
        self._version_enabled: dict[tuple[str, str], bool] = {}
        self._revision = 0

    def register(self, tool: ToolDefinition) -> None:
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(f"工具版本已注册：{tool.name}@{tool.version}")
        self._tools[key] = tool
        self._family_enabled.setdefault(tool.name, True)
        self._version_enabled.setdefault(key, True)
        self._revision += 1

    def set_family_enabled(self, name: str, enabled: bool) -> None:
        self._require_family(name)
        self._family_enabled[name] = enabled
        self._revision += 1

    def set_version_enabled(self, name: str, version: str, enabled: bool) -> None:
        self._require_tool(name, version)
        self._version_enabled[(name, version)] = enabled
        self._revision += 1

    def is_live_enabled(self, tool: ToolDefinition) -> bool:
        return self._family_enabled.get(tool.name, False) and self._version_enabled.get(
            (tool.name, tool.version), False
        )

    def snapshot(self, routes: Mapping[str, str]) -> ToolSnapshot:
        selected: dict[str, ToolDefinition] = {}
        for name, version in routes.items():
            tool = self._require_tool(name, version)
            if not self.is_live_enabled(tool):
                raise ValueError(f"工具当前不可路由：{name}@{version}")
            selected[name] = tool
        return ToolSnapshot(routes=selected, revision=self._revision)

    def _require_family(self, name: str) -> None:
        if not any(registered_name == name for registered_name, _ in self._tools):
            raise KeyError(f"未知工具族：{name}")

    def _require_tool(self, name: str, version: str) -> ToolDefinition:
        try:
            return self._tools[(name, version)]
        except KeyError as error:
            raise KeyError(f"未知工具版本：{name}@{version}") from error


# 针对某个工具、可进一步限定到某类资源目标的规则
@dataclass(frozen=True, slots=True)
class PermissionRule:
    effect: Literal["allow", "deny"]        # 命中规则后的策略结果，允许或拒绝
    tool_name: str                          # 规则作用在哪个工具
    target_prefix: str | None = None        # 规则作用在哪些业务资源


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    action: DecisionAction
    code: str
    reason: str
    source: Literal[
        "rule", "mode", "whitelist", "rbac", "business", "approval", "risk", "default"
    ]


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WaitingForApproval:
    """被冻结的待审批调用；确认后只能恢复这一条原始调用。"""

    call: ToolCall
    tool_version: str
    arguments: ArgsModel
    context: ExecutionContext
    approval_prompt: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    tool_name: str
    ok: bool
    action: DecisionAction
    code: str
    content: Any
    retryable: bool = False

    def to_tool_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": json.dumps(
                {
                    "ok": self.ok,
                    "code": self.code,
                    "action": self.action,
                    "content": _json_value(self.content),
                },
                ensure_ascii=False,
            ),
        }


@dataclass(frozen=True, slots=True)
class AuditRecord:
    trace_id: str
    tool_call_id: str
    tool_name: str
    user_id: str
    tenant_id: str
    phase: Literal["decision", "execution"]
    decision: str
    code: str
    argument_keys: tuple[str, ...]
    latency_ms: int | None = None


@dataclass(slots=True)
class ApprovalRecord:
    approval_id: str
    user_id: str
    tenant_id: str
    tool_name: str
    tool_version: str
    tool_call_id: str
    digest: str
    expires_at: float
    used: bool = False


class ToolExecutionError(RuntimeError):
    """业务 Handler 可安全映射到统一 ToolError 的预期失败。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class PolicyDenied(ToolExecutionError):
    """教学 Demo 中的业务拒绝；兼容原有 Handler / precheck 名称。"""


class ToolError(BaseModel):
    """唯一允许返回模型的安全错误契约。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False
    trace_id: str | None = None


class TransientToolError(RuntimeError):
    pass


def _stable_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _stable_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return value


def _json_value(value: Any) -> Any:
    """把 Pydantic 对象投影为 JSON 可序列化的模型消息内容。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


# 本身不负责防止“多次相同参数的业务操作”。它负责的是另一件事： 防止用户批准 A 动作后，审批被偷换成 B 动作执行。
def _approval_digest(tool_name: str, arguments: ArgsModel | Mapping[str, Any]) -> str:
    canonical = json.dumps(_stable_value(arguments), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


class ApprovalStore:
    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def approve(
        self,
        approval_id: str,
        pending: WaitingForApproval,
        *,
        ttl_seconds: float = 300,
    ) -> None:
        """只为已校验且冻结的原始 ToolCall 创建审批记录。"""

        self._records[approval_id] = ApprovalRecord(
            approval_id=approval_id,
            user_id=pending.context.user_id,
            tenant_id=pending.context.tenant_id,
            tool_name=pending.call.name,
            tool_version=pending.tool_version,
            tool_call_id=pending.call.tool_call_id,
            digest=_approval_digest(pending.call.name, pending.arguments),
            expires_at=time.time() + ttl_seconds,
        )

    def consume(
        self,
        approval_id: str | None,
        context: ExecutionContext,
        tool: ToolDefinition,
        call: ToolCall,
        arguments: ArgsModel,
    ) -> bool:
        record = self._records.get(approval_id or "")
        valid = bool(
            record
            and not record.used
            and record.expires_at >= time.time()
            and record.user_id == context.user_id
            and record.tenant_id == context.tenant_id
            and record.tool_name == tool.name
            and record.tool_version == tool.version
            and record.tool_call_id == call.tool_call_id
            and record.digest == _approval_digest(tool.name, arguments)
        )
        if valid and record:
            record.used = True
        return valid


class AuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


DANGEROUS_SHELL_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bgit\s+push\s+--force\b", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r">\s*/dev/", re.I),
)


def _is_dangerous_shell(command: str) -> bool:
    return any(pattern.search(command) for pattern in DANGEROUS_SHELL_PATTERNS)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***" if re.search(r"token|secret|password|authorization", key, re.I) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", value)
        return re.sub(r"\b(ACC-[A-Z]-)\d{2}(\d{4})\b", r"\1****\2", value)
    return value


class PermissionEngine:
    """固定优先级的三态权限状态机。"""

    def __init__(self, rules: Sequence[PermissionRule], approvals: ApprovalStore) -> None:
        self._rules = tuple(rules)
        self._approvals = approvals

    def _rule_matches(self, rule: PermissionRule, tool: ToolDefinition, arguments: ArgsModel) -> bool:
        if rule.tool_name != tool.name:
            return False
        if rule.target_prefix is None:
            return True
        return tool.canonical_target(arguments).startswith(rule.target_prefix)

    async def decide(
        self,
        tool: ToolDefinition,
        call: ToolCall,
        arguments: ArgsModel,
        context: ExecutionContext,
    ) -> PermissionDecision:
        # 1. deny-first：硬拒绝不能被 allow 或 bypass 覆盖。
        # 只要命中一条 deny，立即拒绝；后面的白名单、RBAC、审批、allow 规则，乃至 BYPASS_PERMISSIONS 都不能覆盖它。
        if any(
            rule.effect == "deny" and self._rule_matches(rule, tool, arguments)
            for rule in self._rules
        ):
            return PermissionDecision(DecisionAction.DENY, "DENY_RULE", "命中 deny 规则", "rule")

        # 2. plan 是执行层只读契约，而不是一句系统提示词。
        if context.mode is PermissionMode.PLAN and tool.policy.effect is not Effect.READ:
            return PermissionDecision(
                DecisionAction.DENY,
                "PLAN_MODE_DENIED",
                "plan 模式禁止写操作和 Shell",
                "mode",
            )

        # 3. 发现阶段过滤后，执行阶段仍然要重新检查白名单。
        if tool.name not in context.allowed_tools:
            return PermissionDecision(
                DecisionAction.DENY,
                "TOOL_NOT_ALLOWED",
                "工具不在本轮执行白名单",
                "whitelist",
            )

        # 4. 只相信认证层生成的 ExecutionContext。
        if tool.policy.permission not in context.permissions:
            return PermissionDecision(
                DecisionAction.DENY,
                "PERMISSION_DENIED",
                f"缺少业务权限 {tool.policy.permission}",
                "rbac",
            )

        # 5. 资源归属、状态和额度在 handler 之前验证；未知异常必须 fail closed。
        try:
            if tool.precheck:
                await tool.precheck(arguments, context)
        except PolicyDenied as error:
            return PermissionDecision(DecisionAction.DENY, error.code, str(error), "business")
        except Exception:
            return PermissionDecision(
                DecisionAction.DENY,
                "PRECHECK_FAILED",
                "业务前置校验暂时不可用",
                "business",
            )

        # 6. 高风险业务写操作必须使用一次性、参数绑定审批。
        if tool.policy.requires_approval or tool.policy.risk is Risk.HIGH:
            if self._approvals.consume(context.approval_id, context, tool, call, arguments):
                return PermissionDecision(
                    DecisionAction.ALLOW,
                    "APPROVED",
                    "审批与当前用户、租户、工具和参数完全匹配",
                    "approval",
                )
            if context.mode is PermissionMode.DONT_ASK:
                return PermissionDecision(
                    DecisionAction.DENY,
                    "APPROVAL_REQUIRED",
                    "非交互模式无法完成高风险确认",
                    "approval",
                )
            return PermissionDecision(
                DecisionAction.CONFIRM,
                "APPROVAL_REQUIRED",
                "需要确认本次具体动作",
                "approval",
            )

        # 7. bypass 只能跳过普通确认，不能跳过前面的硬边界。
        if context.mode is PermissionMode.BYPASS_PERMISSIONS:
            return PermissionDecision(
                DecisionAction.ALLOW,
                "BYPASS_ALLOWED",
                "跳过普通确认，但硬边界已经全部通过",
                "mode",
            )

        # 8. allow 规则只在 deny、plan、白名单、RBAC 和审批以后生效。
        if any(
            rule.effect == "allow" and self._rule_matches(rule, tool, arguments)
            for rule in self._rules
        ):
            return PermissionDecision(DecisionAction.ALLOW, "ALLOW_RULE", "命中 allow 规则", "rule")

        # 9. 正则只是教学兜底，生产中必须配合窄工具、AST 与沙箱。
        if tool.policy.effect is Effect.SHELL and _is_dangerous_shell(
            str(getattr(arguments, "command", ""))
        ):
            if context.mode is PermissionMode.DONT_ASK:
                return PermissionDecision(
                    DecisionAction.DENY,
                    "DANGEROUS_OPERATION",
                    "危险 Shell 在非交互模式下被拒绝",
                    "risk",
                )
            return PermissionDecision(
                DecisionAction.CONFIRM,
                "DANGEROUS_OPERATION",
                "危险 Shell 需要用户确认",
                "risk",
            )

        return PermissionDecision(
            DecisionAction.ALLOW,
            "DEFAULT_ALLOWED",
            "所有确定性检查均已通过",
            "default",
        )


class ToolRuntime:
    """模型、CLI、测试与未来 Provider 共用的唯一工具执行入口。"""

    def __init__(
        self,
        registry: ToolRegistry,
        snapshot: ToolSnapshot,
        permission_engine: PermissionEngine,
        audit_sink: AuditSink,
    ) -> None:
        self._registry = registry
        self._snapshot = snapshot
        self._permission_engine = permission_engine
        self._audit = audit_sink
        self._approvals = permission_engine._approvals
        self._pending: dict[str, WaitingForApproval] = {}

    def model_tools(self, context: ExecutionContext) -> list[dict[str, Any]]:
        """只暴露本 Run Snapshot 中的工具，不暴露全局 Registry 或 version。"""

        return [
            tool.to_model_tool()
            for tool in self._snapshot.routes.values()
            if tool.name in context.allowed_tools
        ]

    async def invoke(self, call: ToolCall, context: ExecutionContext) -> ToolResult:
        started = time.perf_counter()
        tool = self._snapshot.routes.get(call.name)
        if tool is None:
            return self._rejected(call, context, "TOOL_NOT_FOUND", "工具不在当前 Run Snapshot")
        # Runtime 控制面复查：即使 Snapshot 创建后，也能阻断在飞的旧版本。
        if not self._registry.is_live_enabled(tool):
            return self._rejected(call, context, "TOOL_DISABLED", "工具当前已被停用")

        # prepare-1：Pydantic 把不可信字典转换成不可变的规范化业务对象。
        try:
            arguments = tool.parameters_model.model_validate(call.arguments)
        except ValidationError as error:
            details = [
                {"path": ".".join(map(str, item["loc"])), "message": item["msg"]}
                for item in error.errors(include_url=False)
            ]
            return self._rejected(call, context, "INVALID_ARGUMENT", details)

        # prepare-2：执行期重新授权，返回 allow / deny / confirm。
        decision = await self._permission_engine.decide(tool, call, arguments, context)
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="decision",
                decision=decision.action,
                code=decision.code,
                argument_keys=tuple(sorted(call.arguments)),
            )
        )
        if decision.action is not DecisionAction.ALLOW:
            if decision.action is DecisionAction.CONFIRM:
                self._pending[call.tool_call_id] = WaitingForApproval(
                    call=call,
                    tool_version=tool.version,
                    arguments=arguments,
                    context=context,
                    approval_prompt=decision.reason,
                )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                ok=False,
                action=decision.action,
                code=decision.code,
                content=decision.reason,
            )

        # execute：只有通过全部确定性检查后，handler 才可能产生副作用。
        try:
            raw = await self._execute_with_recovery(tool, call.tool_call_id, arguments, context)
        except TimeoutError:
            code = "TIMEOUT" if tool.policy.effect is Effect.READ or tool.policy.idempotent else "TIMEOUT_UNKNOWN"
            return self._failed(call, context, started, ToolError(code=code, message="工具执行超时", trace_id=context.trace_id))
        except ToolExecutionError as error:
            return self._failed(
                call,
                context,
                started,
                ToolError(code=error.code, message=str(error), retryable=error.retryable, trace_id=context.trace_id),
            )
        except Exception:
            return self._failed(
                call,
                context,
                started,
                ToolError(code="TOOL_ERROR", message="工具执行失败", trace_id=context.trace_id),
            )

        # finalize：先验证白名单输出契约，再脱敏，最后形成模型能看见的 ToolResult。
        try:
            output_payload = dict(raw)
            allowed_input_keys = {
                field_name if field.validation_alias is None else str(field.validation_alias)
                for field_name, field in tool.output_model.model_fields.items()
            }
            undeclared_keys = set(output_payload) - allowed_input_keys
            if undeclared_keys:
                raise ValueError("handler returned undeclared fields")
            validated_output = tool.output_model.model_validate(output_payload)
        except (TypeError, ValueError, ValidationError):
            return self._failed(
                call,
                context,
                started,
                ToolError(code="INVALID_TOOL_OUTPUT", message="工具返回了不符合契约的结果", trace_id=context.trace_id),
            )
        safe_content = _redact(validated_output.model_dump(mode="json", by_alias=True))
        latency_ms = round((time.perf_counter() - started) * 1_000)
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="execution",
                decision="executed",
                code="OK",
                argument_keys=tuple(sorted(call.arguments)),
                latency_ms=latency_ms,
            )
        )
        return ToolResult(call.tool_call_id, call.name, True, DecisionAction.ALLOW, "OK", safe_content)

    async def confirm(self, approval_id: str, tool_call_id: str) -> ToolResult:
        """用户确认后，记录审批并立即恢复被冻结的原始调用。"""

        pending = self._pending.pop(tool_call_id, None)
        if pending is None:
            raise KeyError("不存在待审批的工具调用")
        self._approvals.approve(approval_id, pending)
        approved_context = replace(pending.context, approval_id=approval_id)
        return await self.invoke(pending.call, approved_context)

    async def _execute_with_recovery(
        self,
        tool: ToolDefinition,
        tool_call_id: str,
        arguments: ArgsModel,
        context: ExecutionContext,
    ) -> Mapping[str, Any]:
        retries = tool.policy.max_retries if tool.policy.effect is Effect.READ or tool.policy.idempotent else 0
        for attempt in range(retries + 1):
            try:
                async with asyncio.timeout(tool.policy.timeout_seconds):
                    return await tool.handler(tool_call_id, arguments, context)
            except TransientToolError:
                if attempt == retries:
                    raise
                await asyncio.sleep(min(0.05 * (2**attempt), 0.2))
        raise AssertionError("unreachable")

    def _rejected(
        self,
        call: ToolCall,
        context: ExecutionContext,
        code: str,
        content: Any,
    ) -> ToolResult:
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="decision",
                decision="deny",
                code=code,
                argument_keys=tuple(sorted(call.arguments)),
            )
        )
        return ToolResult(call.tool_call_id, call.name, False, DecisionAction.DENY, code, content)

    def _failed(
        self,
        call: ToolCall,
        context: ExecutionContext,
        started: float,
        error: ToolError,
    ) -> ToolResult:
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="execution",
                decision="failed",
                code=error.code,
                argument_keys=tuple(sorted(call.arguments)),
                latency_ms=round((time.perf_counter() - started) * 1_000),
            )
        )
        return ToolResult(
            call.tool_call_id,
            call.name,
            False,
            DecisionAction.DENY,
            error.code,
            error,
            retryable=error.retryable,
        )


ORDERS = {
    ("tenant_a", "ord_1001"): {
        "status": "paid",
        "refundable": 399.0,
        "customer_email": "alice@example.com",
    }
}

ACCOUNTS = {
    ("tenant_a", "ACC-A-123456"): 100_000.0,
    ("tenant_a", "ACC-A-654321"): 5_000.0,
    ("tenant_a", "ACC-A-888888"): 20_000.0,
    ("tenant_b", "ACC-B-111111"): 50_000.0,
}

SIDE_EFFECTS = {"refund_executions": 0, "shell_executions": 0, "transfer_executions": 0}


def reset_side_effects() -> None:
    SIDE_EFFECTS.update(refund_executions=0, shell_executions=0, transfer_executions=0)


async def get_order_handler(
    _tool_call_id: str,
    raw_arguments: ArgsModel,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    arguments = raw_arguments
    assert isinstance(arguments, GetOrderArgs)
    order = ORDERS.get((context.tenant_id, arguments.order_id))
    if not order:
        raise PolicyDenied("ORDER_NOT_FOUND", "当前租户下不存在该订单")
    return dict(order)


async def refund_precheck(raw_arguments: ArgsModel, context: ExecutionContext) -> None:
    arguments = raw_arguments
    assert isinstance(arguments, CreateRefundArgs)
    order = ORDERS.get((context.tenant_id, arguments.order_id))
    if not order or order["status"] != "paid":
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或状态不可退款")
    if arguments.amount > float(order["refundable"]):
        raise PolicyDenied("BUSINESS_RULE_DENIED", "退款金额超过可退金额")

async def transfer_precheck(raw_arguments: ArgsModel, context: ExecutionContext) -> None:
    # 只做判断，不改余额
    arguments = raw_arguments
    assert isinstance(arguments, TransferArgs)
    if arguments.amount > 50_000:
        raise PolicyDenied("EXCEED_LIMIT", "已触发限额")
    from_key = (context.tenant_id, arguments.from_account)
    account_amount = ACCOUNTS.get(from_key, 0.0)
    if account_amount < arguments.amount:
        # 账户金额小于转账金额
        raise PolicyDenied("INSUFFICIENT_BALANCE", "账户余额不足")


async def create_refund_handler(
    tool_call_id: str,
    raw_arguments: ArgsModel,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    arguments = raw_arguments
    assert isinstance(arguments, CreateRefundArgs)
    SIDE_EFFECTS["refund_executions"] += 1
    return {
        "refund_id": "ref_9001",
        "idempotency_key": tool_call_id,
        "tenant_id": context.tenant_id,
        "order_id": arguments.order_id,
        "amount": arguments.amount,
        "status": "accepted",
    }


async def simulated_shell_handler(
    _tool_call_id: str,
    raw_arguments: ArgsModel,
    _context: ExecutionContext,
) -> Mapping[str, Any]:
    arguments = raw_arguments
    assert isinstance(arguments, RunShellArgs)
    SIDE_EFFECTS["shell_executions"] += 1
    return {
        "simulated": True,
        "command": arguments.command,
        "stdout": "教学模拟：没有创建真实子进程",
    }


async def transfer_handler(tool_call_id: str, raw_arguments: ArgsModel, context: ExecutionContext) -> Mapping[str, Any]:
    """教学用非幂等写操作：当前尚未引入 Repository 原子写入与 idempotency key。"""

    arguments = raw_arguments
    assert isinstance(arguments, TransferArgs)
    from_key = (context.tenant_id, arguments.from_account)
    to_key = (context.tenant_id, arguments.to_account)
    if to_key not in ACCOUNTS:
        raise PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")

    # 教学用：在 Runtime 的 2 秒 timeout 内故意等待 3 秒，制造可观察的慢调用。
    if arguments.amount > 40_000:
        await asyncio.sleep(3.0)

    # 真实系统应以数据库事务/条件更新保证这一扣一加的原子性。
    ACCOUNTS[from_key] -= arguments.amount
    ACCOUNTS[to_key] += arguments.amount
    SIDE_EFFECTS["transfer_executions"] += 1
    return {
        "txn_id": tool_call_id[-6:],
        "from": arguments.from_account,
        "to": arguments.to_account,
        "amount": arguments.amount,
        "status": "success",
    }


def build_tools(*, version: str = "v1") -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_order",
            version=version,
            description="查询当前租户订单状态和可退金额",
            parameters_model=GetOrderArgs,
            output_model=GetOrderOutput,
            policy=ToolPolicy(Effect.READ, Risk.MEDIUM, "order:read", False, 1.0, 2, True),
            handler=get_order_handler,
            canonical_target=lambda args: str(getattr(args, "order_id")),
        ),
        ToolDefinition(
            name="create_refund",
            version=version,
            description="为当前租户的已支付订单创建退款",
            parameters_model=CreateRefundArgs,
            output_model=CreateRefundOutput,
            policy=ToolPolicy(Effect.WRITE, Risk.HIGH, "refund:create", True, 2.0, 0, False),
            handler=create_refund_handler,
            precheck=refund_precheck,
            canonical_target=lambda args: f"{getattr(args, 'order_id')}:{getattr(args, 'amount')}",
        ),
        ToolDefinition(
            name="run_shell",
            version=version,
            description="教学用模拟 Shell，不执行真实系统命令",
            parameters_model=RunShellArgs,
            output_model=RunShellOutput,
            policy=ToolPolicy(Effect.SHELL, Risk.MEDIUM, "shell:run", False, 1.0, 0, False),
            handler=simulated_shell_handler,
            canonical_target=lambda args: str(getattr(args, "command")),
        ),
        ToolDefinition(
            name="transfer",
            version=version,
            description="从指定账户操作转账金额至目标账户",
            parameters_model=TransferArgs,
            output_model=TransferOutput,
            policy=ToolPolicy(Effect.WRITE, Risk.HIGH, "transfer:execute", True, 2.0, 0, False),
            handler=transfer_handler,
            precheck=transfer_precheck,
            canonical_target=lambda args: f"{args.from_account}->{args.to_account}",
        )
    ]


DEFAULT_RULES = (
    PermissionRule("deny", "run_shell", "rm -rf"),
    PermissionRule("deny", "run_shell", "git push --force"),
    PermissionRule("allow", "run_shell", "pytest"),
)


def base_context(**overrides: Any) -> ExecutionContext:
    context = ExecutionContext(
        trace_id="trace_demo",
        user_id="u_100",
        tenant_id="tenant_a",
        mode=PermissionMode.DEFAULT,
        permissions=frozenset({"order:read", "refund:create", "shell:run"}),
        allowed_tools=frozenset({"get_order", "create_refund", "run_shell"}),
    )
    return replace(context, **overrides)


def build_registry(*, versions: Sequence[str] = ("v1",)) -> ToolRegistry:
    registry = ToolRegistry()
    for version in versions:
        for tool in build_tools(version=version):
            registry.register(tool)
    return registry


def build_runtime(
    *,
    approvals: ApprovalStore | None = None,
    audit: AuditSink | None = None,
    rules: Sequence[PermissionRule] = DEFAULT_RULES,
    registry: ToolRegistry | None = None,
    routes: Mapping[str, str] | None = None,
) -> tuple[ToolRuntime, ApprovalStore, AuditSink]:
    approval_store = approvals or ApprovalStore()
    audit_sink = audit or AuditSink()
    selected_registry = registry or build_registry()
    selected_routes = routes or {tool.name: tool.version for tool in build_tools()}
    snapshot = selected_registry.snapshot(selected_routes)
    engine = PermissionEngine(rules, approval_store)
    return ToolRuntime(selected_registry, snapshot, engine, audit_sink), approval_store, audit_sink


async def run_offline_demo() -> None:
    reset_side_effects()
    registry = build_registry(versions=("v1", "v2"))
    v1_routes = {tool.name: "v1" for tool in build_tools()}
    v2_routes = {tool.name: "v2" for tool in build_tools()}
    runtime, _, audit = build_runtime(registry=registry, routes=v1_routes)
    future_runtime, _, _ = build_runtime(registry=registry, routes=v2_routes)
    context = base_context()
    refund_arguments = {"order_id": "ord_1001", "amount": 399.0, "reason": "商品存在质量问题"}
    pending_refund = ToolCall("call_02", "create_refund", refund_arguments)

    print("[snapshot] current get_order:", runtime._snapshot.routes["get_order"].version)
    print("[snapshot] future get_order:", future_runtime._snapshot.routes["get_order"].version)
    print("[model schema]", json.dumps(runtime.model_tools(context)[0], ensure_ascii=False))

    results = [
        await runtime.invoke(ToolCall("call_01", "get_order", {"order_id": "ord_1001"}), context),
        await runtime.invoke(pending_refund, context),
        await runtime.confirm("approval_01", pending_refund.tool_call_id),
    ]
    results.extend(
        [
            await runtime.invoke(
                ToolCall(
                    "call_04",
                    "create_refund",
                    {**refund_arguments, "user_id": "admin", "approved": True},
                ),
                context,
            ),
            await runtime.invoke(
                ToolCall("call_05", "run_shell", {"command": "rm -rf /tmp/demo"}),
                replace(context, mode=PermissionMode.BYPASS_PERMISSIONS),
            ),
            await runtime.invoke(
                ToolCall("call_06", "create_refund", refund_arguments),
                replace(context, mode=PermissionMode.PLAN, approval_id="approval_01"),
            ),
        ]
    )

    for result in results:
        print(json.dumps({
            "tool_call_id": result.tool_call_id,
            "tool_name": result.tool_name,
            "ok": result.ok,
            "action": result.action,
            "code": result.code,
            "content": _json_value(result.content),
        }, ensure_ascii=False, default=str))
    registry.set_version_enabled("get_order", "v1", False)
    stale_result = await runtime.invoke(
        ToolCall("call_07", "get_order", {"order_id": "ord_1001"}), context
    )
    v2_result = await future_runtime.invoke(
        ToolCall("call_08", "get_order", {"order_id": "ord_1001"}), context
    )
    print("[live switch] v1 stale snapshot:", stale_result.code)
    print("[live switch] v2 new snapshot:", v2_result.code)
    print(json.dumps({"side_effects": SIDE_EFFECTS, "audit_records": len(audit.records)}, ensure_ascii=False))


async def run_deepseek_agent(user_input: str) -> None:
    """可选真实模型闭环；所有工具调用仍经过同一个 ToolRuntime.invoke。"""

    from openai import AsyncOpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")

    runtime, _, _ = build_runtime()
    context = base_context(allowed_tools=frozenset({"get_order"}))
    client = AsyncOpenAI(api_key=api_key, base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "你是订单助手。只根据工具结果回答，不得伪造订单事实。",
        },
        {"role": "user", "content": user_input},
    ]

    for _round in range(8):
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=runtime.model_tools(context),
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text_parts: list[str] = []
        pending_calls: dict[int, dict[str, Any]] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                print(delta.content, end="", flush=True)
            for delta_call in delta.tool_calls or []:
                current = pending_calls.setdefault(
                    delta_call.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if delta_call.id:
                    current["id"] = delta_call.id
                if delta_call.function:
                    if delta_call.function.name:
                        current["function"]["name"] += delta_call.function.name
                    if delta_call.function.arguments:
                        current["function"]["arguments"] += delta_call.function.arguments

        provider_calls = [pending_calls[index] for index in sorted(pending_calls)]
        assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if provider_calls:
            assistant_message["tool_calls"] = provider_calls
        messages.append(assistant_message)

        if not provider_calls:
            print()
            return

        if text_parts:
            print()
        for provider_call in provider_calls:
            try:
                raw_arguments = json.loads(provider_call["function"]["arguments"])
            except json.JSONDecodeError:
                raw_arguments = {"_invalid_json": provider_call["function"]["arguments"]}
            result = await runtime.invoke(
                ToolCall(provider_call["id"], provider_call["function"]["name"], raw_arguments),
                context,
            )
            print(f"[tool_result] {result.tool_name} {result.code}")
            messages.append(result.to_tool_message())

    raise RuntimeError("Agent Loop 超过最大轮数 8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python 工具治理与权限状态机演示")
    parser.add_argument("--agent", action="store_true", help="使用 DeepSeek 运行真实 Agent Loop")
    parser.add_argument("--input", default="请查询订单 ord_1001 的状态和可退金额")
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(run_deepseek_agent(cli_args.input) if cli_args.agent else run_offline_demo())