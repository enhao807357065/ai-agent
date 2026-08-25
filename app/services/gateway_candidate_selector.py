"""按能力、健康、优先级和成本构造 Gateway 执行候选序列。"""

from __future__ import annotations

from decimal import Decimal

from app.models.capabilities import CandidateTarget, GatewayRequirements, RoutingPolicy, TargetProfile
from app.models.gateway import GatewayModelCall
from app.services.target_health_registry import TargetHealthRegistry


class GatewayCapabilityUnavailable(Exception):
    """没有目标满足请求能力，或满足能力的目标目前均因健康状态不可选。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class GatewayCandidateSelector:
    def __init__(
        self,
        targets: dict[str, TargetProfile],
        policies: dict[str, RoutingPolicy],
        health_registry: TargetHealthRegistry,
    ) -> None:
        self._targets = targets
        self._policies = policies
        self._health = health_registry

    @property
    def logical_models(self) -> list[str]:
        return list(self._policies)

    def resolve_policy(self, logical_model: str | None) -> tuple[str, RoutingPolicy]:
        if not logical_model:
            raise GatewayCapabilityUnavailable("model is required; use a logical model from GET /v1/models")
        policy = self._policies.get(logical_model)
        if policy is None:
            available = ", ".join(self.logical_models)
            raise GatewayCapabilityUnavailable(f"Unknown logical model '{logical_model}'. Available: {available}")
        return logical_model, policy

    @staticmethod
    def _estimate_cost(profile: TargetProfile, call: GatewayModelCall) -> Decimal:
        # Phase 2 的保守估算：字符数 / 4 近似输入 token，最大输出 token 作为预算上界。
        input_chars = sum(len(str(message.get("content") or "")) for message in call.messages)
        estimated_input_tokens = max(1, input_chars // 4)
        return (
            Decimal(estimated_input_tokens) * profile.input_cost_per_million
            + Decimal(call.max_tokens) * profile.output_cost_per_million
        ) / Decimal("1000000")

    def select(
        self,
        logical_model: str | None,
        call: GatewayModelCall,
        requirements: GatewayRequirements,
        *,
        excluded_target_ids: set[str] | None = None,
    ) -> list[CandidateTarget]:
        name, policy = self.resolve_policy(logical_model)
        required = requirements.capabilities | policy.required_capabilities
        excluded = excluded_target_ids or set()
        candidates: list[CandidateTarget] = []
        rejected: dict[str, str] = {}
        saw_compatible_but_unhealthy = False

        for target_id in policy.target_ids:
            profile = self._targets.get(target_id)
            if profile is None:
                rejected[target_id] = "missing_registry_target"
                continue
            if target_id in excluded:
                rejected[target_id] = "already_attempted"
                continue
            if not profile.enabled:
                rejected[target_id] = "disabled"
                continue
            if not required.issubset(profile.capabilities):
                rejected[target_id] = "capability_mismatch"
                continue
            if profile.max_output_tokens is not None and call.max_tokens > profile.max_output_tokens:
                rejected[target_id] = "max_output_tokens"
                continue
            if not self._health.is_eligible(target_id):
                saw_compatible_but_unhealthy = True
                rejected[target_id] = "circuit_open"
                continue

            cost = self._estimate_cost(profile, call)
            if policy.max_estimated_cost is not None and cost > policy.max_estimated_cost:
                rejected[target_id] = "cost_budget_exceeded"
                continue
            candidates.append(CandidateTarget(
                profile=profile,
                estimated_cost=cost,
                health_score=self._health.health_score(target_id),
            ))

        if not candidates:
            required_text = ", ".join(sorted(capability.value for capability in required))
            details = ", ".join(f"{target_id}:{reason}" for target_id, reason in rejected.items())
            raise GatewayCapabilityUnavailable(
                f"No eligible target for '{name}' with capabilities [{required_text}]. {details}",
                retryable=saw_compatible_but_unhealthy,
            )

        # 字典序排序；顺序由 policy 声明，默认 priority → estimated_cost → health。
        def sort_key(candidate: CandidateTarget):
            values: list[object] = []
            for dimension in policy.selection_order:
                if dimension == "priority":
                    values.append(candidate.profile.priority)
                elif dimension == "estimated_cost":
                    values.append(candidate.estimated_cost)
                elif dimension == "health":
                    values.append(-candidate.health_score)
            return (*values, candidate.profile.id)

        candidates.sort(key=sort_key)
        return candidates
