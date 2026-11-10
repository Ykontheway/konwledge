"""Offline metrics for ranked retrieval lineage results."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from uuid import UUID

from konwledge.evaluation.models import AggregateMetrics, ItemMetrics

_NIL_UUID = UUID(int=0)
_RANKING_FIELDS = ("recall", "precision", "reciprocal_rank", "ndcg", "hit_rate")


def _validated_lineages(values: Iterable[UUID], *, name: str) -> tuple[UUID, ...]:
    """Materialize and strictly validate a lineage ID collection."""

    if values is None:
        raise TypeError(f"{name} must be an iterable of UUID values")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of UUID values") from exc

    for lineage_id in materialized:
        if not isinstance(lineage_id, UUID):
            raise TypeError(f"{name} must contain only UUID values")
        if lineage_id == _NIL_UUID:
            raise ValueError(f"{name} must not contain the nil UUID")
    return materialized


def score_ranked_lineages(
    expected_lineage_ids: Iterable[UUID],
    returned_lineage_ids: Sequence[UUID],
    *,
    k: int,
    expect_no_answer: bool = False,
) -> ItemMetrics:
    """Score the first ``k`` ranked slots against distinct expected lineages.

    Duplicate returned IDs retain their ranks and consume slots, but only the
    first occurrence of a relevant lineage receives relevance credit.
    """

    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an integer")
    if k <= 0:
        raise ValueError("k must be positive")
    if not isinstance(expect_no_answer, bool):
        raise TypeError("expect_no_answer must be a boolean")

    expected = _validated_lineages(expected_lineage_ids, name="expected_lineage_ids")
    if len(expected) != len(set(expected)):
        raise ValueError("expected_lineage_ids must not contain duplicates")
    returned = _validated_lineages(returned_lineage_ids, name="returned_lineage_ids")

    if expect_no_answer:
        if expected:
            raise ValueError("no-answer cases require empty expected_lineage_ids")
        return ItemMetrics(
            recall=None,
            precision=None,
            reciprocal_rank=None,
            ndcg=None,
            hit_rate=None,
            no_answer_success=not returned[:k],
        )
    if not expected:
        raise ValueError("answerable cases require at least one expected lineage ID")

    gold = set(expected)
    credited: set[UUID] = set()
    relevant_ranks: list[int] = []
    for rank, lineage_id in enumerate(returned[:k], start=1):
        if lineage_id in gold and lineage_id not in credited:
            credited.add(lineage_id)
            relevant_ranks.append(rank)

    matched = len(credited)
    dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_hits = min(k, len(gold))
    ideal_dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))

    return ItemMetrics(
        recall=matched / len(gold),
        precision=matched / k,
        reciprocal_rank=0.0 if not relevant_ranks else 1.0 / relevant_ranks[0],
        ndcg=dcg / ideal_dcg,
        hit_rate=1.0 if relevant_ranks else 0.0,
        no_answer_success=None,
    )


def _validate_metric_value(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"answerable metric {field} must be defined")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise ValueError(f"answerable metric {field} must be finite and between 0 and 1")
    return numeric


def aggregate_metrics(metrics: Sequence[ItemMetrics]) -> AggregateMetrics:
    """Compute macro means while keeping answerable and no-answer cohorts separate."""

    answerable_values: dict[str, list[float]] = {field: [] for field in _RANKING_FIELDS}
    no_answer_successes: list[bool] = []

    for metric in metrics:
        if not isinstance(metric, ItemMetrics):
            raise TypeError("metrics must contain only ItemMetrics values")

        ranking_values = tuple(getattr(metric, field) for field in _RANKING_FIELDS)
        if metric.no_answer_success is None:
            if any(value is None for value in ranking_values):
                raise ValueError("answerable items require all five ranking metrics")
            for field, value in zip(_RANKING_FIELDS, ranking_values, strict=True):
                answerable_values[field].append(_validate_metric_value(value, field=field))
            continue

        if not isinstance(metric.no_answer_success, bool):
            raise ValueError("no_answer_success must be a boolean for no-answer items")
        if any(value is not None for value in ranking_values):
            raise ValueError("no-answer items require all five ranking metrics to be None")
        no_answer_successes.append(metric.no_answer_success)

    answerable_count = len(answerable_values["recall"])
    no_answer_count = len(no_answer_successes)

    def mean(field: str) -> float | None:
        values = answerable_values[field]
        return None if not values else math.fsum(values) / len(values)

    return AggregateMetrics(
        case_count=answerable_count + no_answer_count,
        answerable_count=answerable_count,
        no_answer_count=no_answer_count,
        mean_recall=mean("recall"),
        mean_precision=mean("precision"),
        mean_reciprocal_rank=mean("reciprocal_rank"),
        mean_ndcg=mean("ndcg"),
        hit_rate=mean("hit_rate"),
        no_answer_accuracy=(
            None
            if not no_answer_successes
            else math.fsum(float(success) for success in no_answer_successes) / no_answer_count
        ),
    )
