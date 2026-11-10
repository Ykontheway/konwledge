"""确定性评估回归比较与失败关闭门禁。

本模块只比较已验证快照，不执行检索、不持久化状态，也不自动更新基线。
相对降幅定义为 ``max(0, baseline - candidate) / baseline``；当基线为零时
定义为零（非负指标不可能从零下降），但绝对下限仍会独立生效。
"""

from __future__ import annotations

import json
import math
from datetime import UTC
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from konwledge.domain.models import KonwledgeModel
from konwledge.evaluation.baselines import EvaluationBaseline, baseline_from_report
from konwledge.evaluation.models import AggregateMetrics, EvaluationReport, ItemMetrics
from konwledge.evaluation.validation import _model_data, _reconstruct

_EPSILON = 1e-12


class _GateModel(KonwledgeModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        revalidate_instances="always",
        str_strip_whitespace=False,
    )


class MetricName(StrEnum):
    """可用于门禁的六个宏平均或单 case 指标。"""

    RECALL = "recall"
    PRECISION = "precision"
    MRR = "mrr"
    NDCG = "ndcg"
    HIT_RATE = "hit_rate"
    NO_ANSWER_ACCURACY = "no_answer_accuracy"


def _validate_optional_score(value: Any, *, name: str) -> Any:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or None")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return value


def _validate_optional_finite(value: Any, *, name: str) -> Any:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or None")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _validate_schema_version(value: Any) -> Any:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


class GateRule(_GateModel):
    """一个指标的绝对下限、绝对降幅和相对降幅约束。"""

    metric: MetricName
    scope: Literal["aggregate", "cases", "both"] = "both"
    min_value: float | None = Field(default=None, ge=0.0, le=1.0)
    max_abs_drop: float | None = Field(default=0.0, ge=0.0, le=1.0)
    max_relative_drop: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("min_value", "max_abs_drop", "max_relative_drop", mode="before")
    @classmethod
    def validate_bounds_finite(cls, value: Any, info: Any) -> Any:
        return _validate_optional_score(value, name=info.field_name)

    @model_validator(mode="after")
    def validate_has_bound(self) -> Self:
        if self.min_value is None and self.max_abs_drop is None and self.max_relative_drop is None:
            raise ValueError("a gate rule must specify at least one bound")
        return self


def _default_rules() -> tuple[GateRule, ...]:
    return tuple(GateRule(metric=metric) for metric in MetricName)


class GatePolicy(_GateModel):
    """门禁策略；默认对六项指标同时检查汇总值与每个 case。"""

    schema_version: Literal[1] = 1
    rules: tuple[GateRule, ...] = Field(default_factory=_default_rules)
    require_same_time: bool = Field(default=False, strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Any:
        return _validate_schema_version(value)

    @field_validator("rules", mode="before")
    @classmethod
    def validate_rule_instances(cls, value: Any) -> Any:
        if isinstance(value, tuple):
            return tuple(
                GateRule.model_validate(_model_data(rule), strict=True)
                if isinstance(rule, GateRule)
                else rule
                for rule in value
            )
        return value

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        if not self.rules:
            raise ValueError("gate policy rules must not be empty")
        metrics = tuple(rule.metric for rule in self.rules)
        if len(metrics) != len(set(metrics)):
            raise ValueError("gate policy must not contain duplicate metric rules")
        return self

    @classmethod
    def default(cls) -> GatePolicy:
        """所有六项指标均不允许绝对降幅的默认严格策略。"""
        return cls(rules=tuple(GateRule(metric=m) for m in MetricName))


class MetricDelta(_GateModel):
    """候选值减基线值；``item_id=None`` 表示汇总指标。"""

    metric: MetricName
    item_id: str | None = None
    baseline_value: float | None
    candidate_value: float | None
    delta: float | None
    relative_delta: float | None

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("item_id must not be blank")
        return value

    @field_validator("baseline_value", "candidate_value", mode="before")
    @classmethod
    def validate_values(cls, value: Any, info: Any) -> Any:
        return _validate_optional_score(value, name=info.field_name)

    @field_validator("delta", "relative_delta", mode="before")
    @classmethod
    def validate_changes(cls, value: Any, info: Any) -> Any:
        return _validate_optional_finite(value, name=info.field_name)

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        baseline = self.baseline_value
        candidate = self.candidate_value
        if (baseline is None) != (candidate is None):
            raise ValueError(
                "baseline and candidate metric values must both be defined or undefined"
            )
        if baseline is None:
            if self.delta is not None or self.relative_delta is not None:
                raise ValueError("undefined metric values require undefined deltas")
            return self

        expected_delta = candidate - baseline  # type: ignore[operator]
        if self.delta is None or abs(self.delta - expected_delta) > _EPSILON:
            raise ValueError("delta does not match candidate_value - baseline_value")
        expected_relative = expected_delta / baseline if baseline > 0.0 else None
        if expected_relative is None:
            if self.relative_delta is not None:
                raise ValueError("relative_delta must be None when baseline_value is zero")
        elif self.relative_delta is None or abs(self.relative_delta - expected_relative) > _EPSILON:
            raise ValueError("relative_delta does not match the metric values")
        return self


class EvaluationComparison(_GateModel):
    """同一数据集快照和检索配置下的完整指标差异。"""

    schema_version: Literal[1] = 1
    baseline: EvaluationBaseline
    candidate: EvaluationBaseline
    deltas: tuple[MetricDelta, ...]

    @model_validator(mode="before")
    @classmethod
    def deserialize_json_snapshots(cls, value: Any) -> Any:
        """让严格的基线模型在嵌套 JSON 中仍使用其 JSON 校验路径。"""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for name in ("baseline", "candidate"):
            snapshot = result.get(name)
            if isinstance(snapshot, dict) and isinstance(snapshot.get("evaluated_at"), str):
                result[name] = EvaluationBaseline.model_validate_json(
                    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                )
        return result

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Any:
        return _validate_schema_version(value)

    @field_validator("baseline", "candidate", mode="before")
    @classmethod
    def validate_snapshots(cls, value: Any) -> Any:
        if isinstance(value, EvaluationBaseline):
            return _reconstruct(value, EvaluationBaseline)
        return value

    @field_validator("deltas", mode="before")
    @classmethod
    def validate_delta_instances(cls, value: Any) -> Any:
        if isinstance(value, tuple):
            return tuple(
                MetricDelta.model_validate(_model_data(delta), strict=True)
                if isinstance(delta, MetricDelta)
                else delta
                for delta in value
            )
        return value

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        _validate_compatible_snapshots(self.baseline, self.candidate)
        expected = _expected_deltas(self.baseline, self.candidate)
        if self.deltas != expected:
            raise ValueError("comparison deltas do not match the validated snapshots")
        return self

    @property
    def degraded(self) -> bool:
        """候选快照是否有缺失向量。"""
        return self.candidate.degraded


class GateCheck(_GateModel):
    """一条规则在一个汇总点或 case 上的可审计结果。"""

    metric: MetricName
    item_id: str | None
    baseline_value: float | None
    candidate_value: float | None
    passed: bool | None
    violations: tuple[str, ...]

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("item_id must not be blank")
        return value

    @field_validator("baseline_value", "candidate_value", mode="before")
    @classmethod
    def validate_values(cls, value: Any, info: Any) -> Any:
        return _validate_optional_score(value, name=info.field_name)

    @field_validator("passed", mode="before")
    @classmethod
    def validate_passed(cls, value: Any) -> Any:
        if value is not None and type(value) is not bool:
            raise ValueError("passed must be a boolean or None")
        return value

    @field_validator("violations")
    @classmethod
    def validate_violations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() for entry in value):
            raise ValueError("violations must not contain blank descriptions")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        undefined = self.baseline_value is None and self.candidate_value is None
        if (self.baseline_value is None) != (self.candidate_value is None):
            raise ValueError("check metric values must both be defined or undefined")
        if undefined:
            if self.passed is not None or self.violations:
                raise ValueError("an undefined check must be not applicable without violations")
        elif self.passed is None:
            raise ValueError("a numeric check must have a boolean result")
        elif self.passed is not (not self.violations):
            raise ValueError("check result is inconsistent with its violations")
        return self


class GateResult(_GateModel):
    """最终门禁判定、逐项检查及全局失败原因。"""

    schema_version: Literal[1] = 1
    comparison: EvaluationComparison
    policy: GatePolicy
    checks: tuple[GateCheck, ...]
    violations: tuple[str, ...]
    passed: bool = Field(strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Any:
        return _validate_schema_version(value)

    @field_validator("comparison", mode="before")
    @classmethod
    def validate_comparison_instance(cls, value: Any) -> Any:
        if isinstance(value, EvaluationComparison):
            return EvaluationComparison.model_validate(_model_data(value), strict=True)
        return value

    @field_validator("policy", mode="before")
    @classmethod
    def validate_policy_instance(cls, value: Any) -> Any:
        if isinstance(value, GatePolicy):
            return GatePolicy.model_validate(_model_data(value), strict=True)
        return value

    @field_validator("checks", mode="before")
    @classmethod
    def validate_check_instances(cls, value: Any) -> Any:
        if isinstance(value, tuple):
            return tuple(
                GateCheck.model_validate(_model_data(check), strict=True)
                if isinstance(check, GateCheck)
                else check
                for check in value
            )
        return value

    @field_validator("violations")
    @classmethod
    def validate_violations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() for entry in value):
            raise ValueError("violations must not contain blank descriptions")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        expected = not self.violations and all(check.passed is not False for check in self.checks)
        if self.passed is not expected:
            raise ValueError("gate result passed flag is inconsistent with checks and violations")
        return self


def _validate_compatible_snapshots(
    baseline: EvaluationBaseline, candidate: EvaluationBaseline
) -> None:
    if baseline.degraded:
        raise ValueError("baseline must be healthy and contain no missing vectors")
    if (
        baseline.dataset.id != candidate.dataset.id
        or baseline.dataset.version != candidate.dataset.version
        or baseline.dataset_hash != candidate.dataset_hash
        or baseline.dataset != candidate.dataset
    ):
        raise ValueError("candidate dataset snapshot does not match baseline")
    for field in ("mode", "top_k", "candidate_limit", "rrf_k"):
        if getattr(baseline, field) != getattr(candidate, field):
            raise ValueError(f"candidate {field} does not match baseline")


def _metric_delta(
    metric: MetricName,
    item_id: str | None,
    baseline_value: float | None,
    candidate_value: float | None,
) -> MetricDelta:
    if (baseline_value is None) != (candidate_value is None):
        raise ValueError(f"metric {metric.value} presence differs between baseline and candidate")
    if baseline_value is None:
        return MetricDelta(
            metric=metric,
            item_id=item_id,
            baseline_value=None,
            candidate_value=None,
            delta=None,
            relative_delta=None,
        )
    delta = candidate_value - baseline_value  # type: ignore[operator]
    relative = delta / baseline_value if baseline_value > 0.0 else None
    return MetricDelta(
        metric=metric,
        item_id=item_id,
        baseline_value=baseline_value,
        candidate_value=candidate_value,
        delta=delta,
        relative_delta=relative,
    )


def _aggregate_values(metrics: AggregateMetrics) -> tuple[float | None, ...]:
    return (
        metrics.mean_recall,
        metrics.mean_precision,
        metrics.mean_reciprocal_rank,
        metrics.mean_ndcg,
        metrics.hit_rate,
        metrics.no_answer_accuracy,
    )


def _case_values(metrics: ItemMetrics) -> tuple[float | None, ...]:
    no_answer = None if metrics.no_answer_success is None else float(metrics.no_answer_success)
    return (
        metrics.recall,
        metrics.precision,
        metrics.reciprocal_rank,
        metrics.ndcg,
        metrics.hit_rate,
        no_answer,
    )


def _expected_deltas(
    baseline: EvaluationBaseline, candidate: EvaluationBaseline
) -> tuple[MetricDelta, ...]:
    deltas = [
        _metric_delta(metric, None, baseline_value, candidate_value)
        for metric, baseline_value, candidate_value in zip(
            MetricName,
            _aggregate_values(baseline.metrics),
            _aggregate_values(candidate.metrics),
            strict=True,
        )
    ]
    for baseline_case, candidate_case in zip(baseline.cases, candidate.cases, strict=True):
        deltas.extend(
            _metric_delta(metric, baseline_case.item_id, baseline_value, candidate_value)
            for metric, baseline_value, candidate_value in zip(
                MetricName,
                _case_values(baseline_case.metrics),
                _case_values(candidate_case.metrics),
                strict=True,
            )
        )
    return tuple(deltas)


def _comparison_from_snapshots(
    baseline: EvaluationBaseline, candidate: EvaluationBaseline
) -> EvaluationComparison:
    _validate_compatible_snapshots(baseline, candidate)
    return EvaluationComparison(
        baseline=baseline,
        candidate=candidate,
        deltas=_expected_deltas(baseline, candidate),
    )


def compare_baseline(
    baseline: EvaluationBaseline, candidate: EvaluationReport
) -> EvaluationComparison:
    """完整复验输入，并比较同一 gold 快照和检索配置。"""
    checked_baseline = _reconstruct(baseline, EvaluationBaseline)
    checked_candidate = baseline_from_report(candidate, allow_degraded=True)
    return _comparison_from_snapshots(checked_baseline, checked_candidate)


def _location(delta: MetricDelta) -> str:
    if delta.item_id is None:
        return f"aggregate {delta.metric.value}"
    return f"case {delta.item_id} {delta.metric.value}"


def evaluate_gate(comparison: EvaluationComparison, policy: GatePolicy | None = None) -> GateResult:
    """重新构造并核对比较内容后执行策略；任何覆盖缺口都会失败关闭。"""
    checked_comparison = EvaluationComparison.model_validate(_model_data(comparison), strict=True)
    reconstructed = _comparison_from_snapshots(
        checked_comparison.baseline, checked_comparison.candidate
    )
    if checked_comparison.deltas != reconstructed.deltas:
        raise ValueError("comparison deltas do not match the validated snapshots")

    selected = GatePolicy() if policy is None else policy
    checked_policy = GatePolicy.model_validate(_model_data(selected), strict=True)
    checks: list[GateCheck] = []
    violations: list[str] = []

    if reconstructed.degraded:
        missing = sorted(
            {
                str(vector_id)
                for case in reconstructed.candidate.cases
                for vector_id in case.missing_vector_ids
            }
        )
        violations.append(f"candidate has missing vectors: {', '.join(missing)}")

    if checked_policy.require_same_time:
        for baseline_case, candidate_case in zip(
            reconstructed.baseline.cases,
            reconstructed.candidate.cases,
            strict=True,
        ):
            if baseline_case.evaluated_at.astimezone(UTC) != candidate_case.evaluated_at.astimezone(
                UTC
            ):
                violations.append(
                    f"case {baseline_case.item_id} effective UTC evaluation time differs"
                )

    rule_by_metric = {rule.metric: rule for rule in checked_policy.rules}
    applicable_count = 0
    for delta in reconstructed.deltas:
        rule = rule_by_metric.get(delta.metric)
        if rule is None:
            continue
        aggregate = delta.item_id is None
        if aggregate and rule.scope not in ("aggregate", "both"):
            continue
        if not aggregate and rule.scope not in ("cases", "both"):
            continue

        if delta.baseline_value is None:
            checks.append(
                GateCheck(
                    metric=delta.metric,
                    item_id=delta.item_id,
                    baseline_value=None,
                    candidate_value=None,
                    passed=None,
                    violations=(),
                )
            )
            continue

        applicable_count += 1
        baseline_value = delta.baseline_value
        candidate_value = delta.candidate_value
        assert candidate_value is not None and delta.delta is not None
        failed: list[str] = []
        if rule.min_value is not None and candidate_value < rule.min_value - _EPSILON:
            failed.append(
                f"candidate value {candidate_value!r} is below minimum {rule.min_value!r}"
            )
        absolute_drop = max(0.0, -delta.delta)
        if rule.max_abs_drop is not None and absolute_drop > rule.max_abs_drop + _EPSILON:
            failed.append(f"absolute drop {absolute_drop!r} exceeds {rule.max_abs_drop!r}")
        relative_drop = (
            max(0.0, baseline_value - candidate_value) / baseline_value
            if baseline_value > 0.0
            else 0.0
        )
        if rule.max_relative_drop is not None and relative_drop > rule.max_relative_drop + _EPSILON:
            failed.append(f"relative drop {relative_drop!r} exceeds {rule.max_relative_drop!r}")

        check = GateCheck(
            metric=delta.metric,
            item_id=delta.item_id,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            passed=not failed,
            violations=tuple(failed),
        )
        checks.append(check)
        violations.extend(f"{_location(delta)}: {failure}" for failure in failed)

    if applicable_count == 0:
        violations.append("policy produced no applicable numeric checks")

    passed = not violations and all(check.passed is not False for check in checks)
    return GateResult(
        comparison=reconstructed,
        policy=checked_policy,
        checks=tuple(checks),
        violations=tuple(violations),
        passed=passed,
    )


def dump_gate_policy(policy: GatePolicy) -> str:
    """输出排序键、缩进 2、拒绝 NaN 且仅一个尾换行的策略 JSON。"""
    checked = GatePolicy.model_validate(_model_data(policy), strict=True)
    return (
        json.dumps(
            checked.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
