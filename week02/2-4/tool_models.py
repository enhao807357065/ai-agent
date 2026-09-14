"""2-4：工具治理的纯数据模型与接口。

本模块只定义运行侧模型与协议：`ToolDefinition` 保存完整服务端定义，
`to_model_tool()` 仅导出 LLM 可见 Schema，`ExecutionContext` 必须由服务端构造。

调用期安全（任一拒绝即短路）：

    Tool Call（不可信）
      → SCHEMA             字段、类型、格式、范围
      → LIVE_SWITCH        Registry / Runtime 检查实时开关、风控、环境边界
      → PLAN_MODE          PLAN 禁止 WRITE / SHELL
      → ALLOW_LIST         工具必须在 context.allow_tools
      → RBAC               当前主体具备所需权限
      → PRECHECK           校验资源归属、状态、额度、依赖事实
      → REQUIRED_APPROVAL  审批/确认必须匹配当前具体动作
      → BYPASS_CONFIRMATION 仅跳过普通确认
      → HANDLER            最终写入仍须原子校验

工具治理基线：
- 模型只能调用收窄后的业务工具；不得直接获得 SQL、Shell 或基础设施凭据。
- 重试须依据幂等性和失败位置；超时不等于可安全重试。
- 结果须按模型、用户、日志分别限长、脱敏和序列化，禁止原样多路传播。
- Prompt 注入不能改变 Runtime 权限：Prompt 区分指令与不可信数据；网页、邮件、Tool Result
  要标记来源、限长、隔离；每次 Tool Call 仍重新经过上述门禁。
- 权限决定“该不该做”；Sandbox 决定“失控时最多伤到哪里”。
- 还应具备超时、限流、并发限制、预算、审计、版本/熔断开关、输出校验和幂等控制。

`BYPASS_PERMISSIONS` 是历史命名：不能绕过白名单、RBAC、业务检查或强制审批。
`DONT_ASK` 遇到需要确认的操作必须拒绝。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict


class ToolArguments(BaseModel):
    """所有工具参数模型的基类。

    Tool call arguments 来自模型，属于不可信协议输入；拒绝未声明字段，
    并由具体子类定义业务字段、类型、范围和格式约束。
    """

    model_config = ConfigDict(extra="forbid")


# 工具影响：调用这个工具会造成哪一类副作用。它不是权限，也不是风险，而是先给工具贴上“行为类别”标签。
class ToolEffect(StrEnum):
    """工具副作用类别；供未来的治理策略和 Runtime 使用。"""

    READ = "read"   # 含义：只读取，不改变外部状态。比如查订单、查库存、搜索知识库。通常的治理方式：可以自动执行，限流、脱敏、租户隔离
    WRITE = "write" # 含义：会改变业务数据或触发外部动作。比如退款、创建订单、发券、发邮件。通常的治理方式：可能需要确认/审批、审计、幂等键
    SHELL = "shell" # 含义：运行系统命令，比如git commit、执行部署脚本。通常的治理方式：高危沙箱、命令白名单、工作目录限制


class RiskLevel(StrEnum):
    """工具风险分级；风险分类本身不等同于审批决定。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# 是一次Agent Run的全局运行档位
# 模式不是权限，代表本次运行的总策略档位
class ExecutionMode(StrEnum):
    """一次 Agent Run 的全局交互档位；模型不能通过参数修改它。

    `BYPASS_PERMISSIONS` 为兼容题目保留的名称，但它只跳过普通用户确认，
    不会跳过白名单、RBAC、业务检查或强制审批。
    """

    DEFAULT = "default"
    PLAN = "plan"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dont_ask"


class PermissionStage(StrEnum):
    """PermissionEngine 内部的有序门禁阶段。

    实时工具开关、环境限制与风控禁用属于 Registry / Runtime 的控制面；
    它们在调用本 Engine 前执行，因此不属于本枚举。
    """

    PLAN_MODE = "plan_mode"
    ALLOW_LIST = "allow_list"
    RBAC = "rbac"
    PRECHECK = "precheck"
    REQUIRED_APPROVAL = "required_approval"
    BYPASS_CONFIRMATION = "bypass_confirmation"
    FINAL_INTERACTION = "final_interaction"


class PermissionDecision(StrEnum):
    """权限引擎的最终结论。"""

    ALLOW = "allow"
    CONFIRM = "confirm"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class AuditPhase(StrEnum):
    """工具调用所处的审计阶段。"""

    DECISION = "decision"
    EXECUTION = "execution"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """最小、不可变且适合精确断言的工具调用审计记录。

    仅保存可信身份、稳定决策码与参数键名；不得记录 prompt、token、参数值、
    密码、原始异常、SQL、Shell 命令或完整执行结果。
    """

    trace_id: str
    tool_call_id: str
    tool_name: str
    user_id: str
    tenant_id: str
    phase: AuditPhase
    decision: PermissionDecision
    code: str
    argument_keys: frozenset[str]
    latency_ms: int | None = None


class AuditSink:
    """进程内审计存储，仅提供追加和按 trace 查询。

    不打印任何记录；生产环境应替换为具备访问控制、持久化和保留策略的后端。
    """

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self._records.append(record)

    def by_trace(self, trace_id: str) -> tuple[AuditRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.trace_id == trace_id
        )


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """可审计的门禁决策结果。

    `stage` 指向终止决策的阶段；`reason_code` 应是稳定的机器可读代码，
    而不是向模型泄露内部实现或资源状态的异常文本。
    """

    decision: PermissionDecision
    stage: PermissionStage
    reason_code: str


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """一次执行的服务端可信上下文。

    禁止从模型 Tool Call arguments 中读取或覆盖本对象中的身份、租户、
    权限、工具白名单、模式或审批信息。
    """

    trace_id: str
    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    allow_tools: frozenset[str]
    mode: ExecutionMode = ExecutionMode.DEFAULT
    approval_id: str | None = None  # approval_id 不应该被理解为“当前 Run 拥有的一组通用通行证”。审批必须精确绑定到某一个不可变的“待执行动作”。


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """工具固有且稳定的治理声明，不包含运行期可变状态。

    工具家族/版本开关、环境限制、租户灰度和风控临时禁用由 Registry 或独立
    Control Plane 管理；Runtime 在创建 Snapshot 与真正调用前都应实时检查。
    """

    effect: ToolEffect
    risk: RiskLevel
    required_permissions: frozenset[str]
    requires_confirmation: bool = False
    requires_approval: bool = False


# Handler / precheck / canonical target 都在运行侧使用，LLM 不可见。
ToolHandler: TypeAlias = Callable[
    [ToolArguments, ExecutionContext],
    Awaitable[Mapping[str, Any]],
]
ToolPrecheck: TypeAlias = Callable[
    [ToolArguments, ExecutionContext],
    Awaitable[None],
]
CanonicalTarget: TypeAlias = Callable[[ToolArguments, ExecutionContext], str]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """服务端拥有的完整工具注册定义。

    parameters_model 同时承担两项职责：
    1. model_json_schema() 产生模型可见的 JSON Schema；
    2. model_validate() 在未来 Runtime 中校验模型生成的 arguments。
    """

    name: str
    description: str
    parameters_model: type[ToolArguments]
    tool_policy: ToolPolicy
    handler: ToolHandler
    precheck: ToolPrecheck | None       # 业务前置检查，如订单是否存在、是否属于当前tenant、当前用户对订单是否有访问资格
    canonical_target: CanonicalTarget   # （规范化目标），把模型乱七八糟说的参数，收成服务端认的「唯一目标 ID」。3个重要用途：1.权限判断更精确、2.审批绑定到具体目标、3.审计日志统一。最重要的用途：防止“审批被偷换目标”

    def to_model_tool(self) -> dict[str, Any]:
        """返回唯一允许暴露给模型的 Function Calling Schema。

        故意不包含 handler、tool_policy、权限、审批、precheck 或
        canonical_target 等运行侧实现与治理信息。
        """

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


class ApprovalVerifier(Protocol):
    """审批校验接口。

    生产实现必须将 approval_id 绑定到当前主体、租户、工具、规范化目标、
    参数哈希、过期时间与一次性消费状态；不能只因 approval_id 非空就放行。
    """

    async def is_approved(
        self,
        tool: ToolDefinition,
        arguments: ToolArguments,
        context: ExecutionContext,
    ) -> bool:
        ...


class PrecheckDenied(Exception):
    """业务预检查可安全暴露的拒绝结果。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PermissionEngine:
    """按固定顺序执行八阶段门禁的学习版实现。

    调用方必须先完成 Pydantic schema 校验，再传入 `ToolArguments`。实时工具
    开关应由 Registry / Runtime 在调用本类之前检查。本类不执行 handler，也不把
    原始异常、资源详情或审批凭据泄露到 `PermissionResult`。
    """

    def __init__(self, approval_verifier: ApprovalVerifier | None = None) -> None:
        self._approval_verifier = approval_verifier

    async def decide(
        self,
        tool: ToolDefinition,
        arguments: ToolArguments,
        context: ExecutionContext,
    ) -> PermissionResult:
        """返回首次终止的门禁结果，不执行 handler。

        `arguments` 必须是 `tool.parameters_model.model_validate()` 产生的对象：
        schema 校验属于 Runtime 的输入边界，必须发生在本方法之前。这样
        precheck / approval verifier 永远不会收到未校验的模型原始 JSON。
        """
        policy = tool.tool_policy

        # 实时开关已由 Registry / Runtime 在进入本 Engine 前检查；此处只处理
        # 静态 ToolPolicy 与本次可信 ExecutionContext 相关的门禁。

        # 1. PLAN_MODE：规划可读，不能产生 WRITE / SHELL 副作用。
        if (
            context.mode is ExecutionMode.PLAN
            and policy.effect in {ToolEffect.WRITE, ToolEffect.SHELL}
        ):
            return self._deny(PermissionStage.PLAN_MODE, "PLAN_MODE_FORBIDDEN")

        # 2. ALLOW_LIST：本次 Run 只拥有完成当前任务所需的最小工具集。
        if tool.name not in context.allow_tools:
            return self._deny(PermissionStage.ALLOW_LIST, "TOOL_NOT_ALLOWED")

        # 3. RBAC：空 required_permissions 表示不额外要求 capability。
        if not policy.required_permissions.issubset(context.permissions):
            return self._deny(PermissionStage.RBAC, "PERMISSION_DENIED")

        # 4. PRECHECK：提前检查业务事实；handler/repository 仍需最终原子校验。
        if tool.precheck is not None:
            try:
                await tool.precheck(arguments, context)
            except PrecheckDenied as exc:
                return self._deny(PermissionStage.PRECHECK, exc.code)
            except Exception:
                # 不把数据库、网络或业务内部异常文本传播给调用方。
                return self._deny(PermissionStage.PRECHECK, "PRECHECK_FAILED")

        # 5. REQUIRED_APPROVAL：审批必须针对当前调用验证，而非仅检查 ID 存在。
        if policy.requires_approval:
            if context.approval_id is None:
                return PermissionResult(
                    decision=PermissionDecision.APPROVAL_REQUIRED,
                    stage=PermissionStage.REQUIRED_APPROVAL,
                    reason_code="APPROVAL_REQUIRED",
                )
            if self._approval_verifier is None:
                return self._deny(
                    PermissionStage.REQUIRED_APPROVAL,
                    "APPROVAL_VERIFIER_UNCONFIGURED",
                )
            if not await self._is_approved(tool, arguments, context):
                return self._deny(PermissionStage.REQUIRED_APPROVAL, "APPROVAL_INVALID")

        # 6. BYPASS_CONFIRMATION：历史模式仅可绕过普通确认，不能越过前五关。
        if (
            policy.requires_confirmation
            and context.mode is ExecutionMode.BYPASS_PERMISSIONS
        ):
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                stage=PermissionStage.BYPASS_CONFIRMATION,
                reason_code="CONFIRMATION_BYPASSED",
            )

        # 7. FINAL_INTERACTION：需要确认但通道不可交互时，拒绝而非默认同意。
        if policy.requires_confirmation:
            if context.mode is ExecutionMode.DONT_ASK:
                return self._deny(
                    PermissionStage.FINAL_INTERACTION,
                    "CONFIRMATION_UNAVAILABLE",
                )
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                stage=PermissionStage.FINAL_INTERACTION,
                reason_code="CONFIRMATION_REQUIRED",
            )

        return PermissionResult(
            decision=PermissionDecision.ALLOW,
            stage=PermissionStage.FINAL_INTERACTION,
            reason_code="ALLOWED",
        )

    async def _is_approved(
        self,
        tool: ToolDefinition,
        arguments: ToolArguments,
        context: ExecutionContext,
    ) -> bool:
        """调用外置 verifier，并把 verifier 故障安全地视作审批不通过。"""
        assert self._approval_verifier is not None
        try:
            return await self._approval_verifier.is_approved(tool, arguments, context)
        except Exception:
            return False

    @staticmethod
    def _deny(stage: PermissionStage, reason_code: str) -> PermissionResult:
        return PermissionResult(
            decision=PermissionDecision.DENY,
            stage=stage,
            reason_code=reason_code,
        )


class ToolRuntime(Protocol):
    """工具执行接口；本练习只定义契约，不提供实现。"""

    async def execute(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        context: ExecutionContext,
    ) -> Mapping[str, Any]:
        ...
