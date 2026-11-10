"""评估边界的纯校验，不改变 U15 报告模型或 JSON 契约。

校验仅证明报告内部结构、证据和重算指标一致，并非密码学来源证明。
这里不读取数据库、不发起网络请求，也不调用检索、向量提供者或生成模型。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel

from konwledge.context.citations import validate_hit_evidence
from konwledge.domain.models import KnowledgeUnitStatus
from konwledge.evaluation.metrics import aggregate_metrics, score_ranked_lineages
from konwledge.evaluation.models import AggregateMetrics, EvaluationReport, ItemMetrics
from konwledge.retrieval.models import RetrievalMode

_METRIC_EPSILON = 1e-12
_Model = TypeVar("_Model", bound=BaseModel)


def _model_data(value: Any) -> Any:
    """展开全部嵌套模型，包括 model_copy 注入的未知字段，不信任实例缓存。

    保留 Python UUID、枚举和 datetime（含 DST fold），不经过 JSON 或字段序列化器。
    """
    if isinstance(value, BaseModel):
        fields = dict(value.__dict__)
        if value.__pydantic_extra__:
            fields.update(value.__pydantic_extra__)
        return {key: _model_data(entry) for key, entry in fields.items()}
    if isinstance(value, dict):
        return {key: _model_data(entry) for key, entry in value.items()}
    if isinstance(value, tuple):
        return tuple(_model_data(entry) for entry in value)
    if isinstance(value, list):
        return [_model_data(entry) for entry in value]
    if isinstance(value, frozenset):
        return frozenset(_model_data(entry) for entry in value)
    if isinstance(value, set):
        return {_model_data(entry) for entry in value}
    return value


def _reconstruct(value: _Model, model: type[_Model]) -> _Model:
    if not isinstance(value, model):
        raise ValueError(f"expected {model.__name__}")
    try:
        return model.model_validate(_model_data(value), strict=True)
    except (TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"invalid {model.__name__}: {exc}") from exc


def _same_instant(actual: datetime, expected: datetime, *, name: str) -> None:
    for value in (actual, expected):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include timezone information")
    # datetime 同一 ZoneInfo 下的直接比较会忽略 fold，必须比较 UTC 时刻。
    if actual.astimezone(UTC) != expected.astimezone(UTC):
        raise ValueError(f"{name} does not match the effective evaluation time")


def _validate_ids(ids: tuple[UUID, ...], *, name: str, unique: bool = False) -> None:
    if not isinstance(ids, tuple):
        raise ValueError(f"{name} must be a tuple of UUID values")
    if any(not isinstance(value, UUID) or value.int == 0 for value in ids):
        raise ValueError(f"{name} must contain non-nil UUID values")
    if unique and len(set(ids)) != len(ids):
        raise ValueError(f"{name} must not contain duplicates")


def _check_metrics(
    actual: ItemMetrics | AggregateMetrics,
    expected: ItemMetrics | AggregateMetrics,
    *,
    name: str,
) -> None:
    """数值指标绝对误差 <= 1e-12；计数、布尔值和缺失值必须完全一致。"""
    if type(actual) is not type(expected):
        raise ValueError(f"{name} has the wrong metric type")
    for field in type(expected).model_fields:
        claimed = getattr(actual, field)
        computed = getattr(expected, field)
        if computed is None:
            valid = claimed is None
        elif isinstance(computed, (bool, int)):
            valid = type(claimed) is type(computed) and claimed == computed
        else:
            valid = (
                not isinstance(claimed, bool)
                and isinstance(claimed, (int, float))
                and math.isfinite(claimed)
                and 0.0 <= claimed <= 1.0
                and abs(claimed - computed) <= _METRIC_EPSILON
            )
        if not valid:
            raise ValueError(f"{name}.{field} does not match recomputed metrics")


def validate_evaluation_report(report: EvaluationReport) -> EvaluationReport:
    """重建并完整验证报告，返回独立验证后的报告；失败抛出 ValueError。

    只检查所声明的生命周期状态是否允许，不从外部重建历史或证明报告来源。
    保留 gold query 原始空白，仅在查询对应关系比较时使用 strip()。
    """
    checked = _reconstruct(report, EvaluationReport)
    if tuple(case.item_id for case in checked.results) != tuple(
        item.id for item in checked.dataset.items
    ):
        raise ValueError("report results must exactly match dataset item IDs and order")

    scores: list[ItemMetrics] = []
    for item, case in zip(checked.dataset.items, checked.results, strict=True):
        retrieval = case.retrieval
        for field in ("mode", "top_k", "candidate_limit", "rrf_k"):
            if getattr(retrieval, field) != getattr(checked, field):
                raise ValueError(f"case {item.id}: retrieval.{field} does not match report")
        if retrieval.include_stale != item.include_stale:
            raise ValueError(f"case {item.id}: retrieval.include_stale does not match item")
        if retrieval.query.strip() != item.query.strip():
            raise ValueError(f"case {item.id}: retrieval.query does not match item")
        _same_instant(
            retrieval.evaluated_at,
            item.at if item.at is not None else checked.evaluated_at,
            name=f"case {item.id}: evaluated_at",
        )
        if len(retrieval.hits) > checked.top_k:
            raise ValueError(f"case {item.id}: hit count exceeds top_k")
        _validate_ids(
            retrieval.missing_vector_ids, name=f"case {item.id}: missing_vector_ids", unique=True
        )
        if checked.mode is RetrievalMode.LEXICAL and retrieval.missing_vector_ids:
            raise ValueError(f"case {item.id}: lexical retrieval cannot have missing vectors")

        unit_ids: set[UUID] = set()
        lineages: list[UUID] = []
        for hit in retrieval.hits:
            validate_hit_evidence(hit)
            _validate_ids(
                (hit.source.id, hit.document.id, hit.unit.id, hit.unit.lineage_id),
                name=f"case {item.id}: hit IDs",
            )
            if item.source_id is not None and hit.source.id != item.source_id:
                raise ValueError(f"case {item.id}: hit source does not match source filter")
            if hit.status is not KnowledgeUnitStatus.ACTIVE and not (
                item.include_stale and hit.status is KnowledgeUnitStatus.STALE
            ):
                raise ValueError(f"case {item.id}: hit status is not allowed")
            if hit.unit.id in unit_ids:
                raise ValueError(f"case {item.id}: duplicate hit unit IDs")
            unit_ids.add(hit.unit.id)
            lineages.append(hit.unit.lineage_id)

        score = score_ranked_lineages(
            item.expected_lineage_ids,
            tuple(lineages),
            k=checked.top_k,
            expect_no_answer=item.expect_no_answer,
        )
        _check_metrics(case.metrics, score, name=f"case {item.id}: metrics")
        scores.append(score)
    _check_metrics(checked.metrics, aggregate_metrics(scores), name="report metrics")
    return checked
