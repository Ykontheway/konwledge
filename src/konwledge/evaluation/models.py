"""Immutable public values returned by retrieval evaluation."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from konwledge.domain import EvaluationSet
from konwledge.domain.models import KonwledgeModel
from konwledge.retrieval import RetrievalMode, RetrievalResult

Metric = float | None


class ItemMetrics(KonwledgeModel):
    """Retrieval metrics for one answerable or no-answer case."""

    recall: Metric
    precision: Metric
    reciprocal_rank: Metric
    ndcg: Metric
    hit_rate: Metric
    no_answer_success: bool | None


class AggregateMetrics(KonwledgeModel):
    """Macro metrics, separating answerable and no-answer populations."""

    case_count: int = Field(strict=True, ge=0)
    answerable_count: int = Field(strict=True, ge=0)
    no_answer_count: int = Field(strict=True, ge=0)
    mean_recall: Metric
    mean_precision: Metric
    mean_reciprocal_rank: Metric
    mean_ndcg: Metric
    hit_rate: Metric
    no_answer_accuracy: Metric


class EvaluationCaseResult(KonwledgeModel):
    """The complete retrieval result and score for one stable case key."""

    model_config = ConfigDict(strict=True, str_strip_whitespace=False)

    item_id: str = Field(min_length=1)
    retrieval: RetrievalResult
    metrics: ItemMetrics


class EvaluationReport(KonwledgeModel):
    """Portable result of evaluating one immutable dataset snapshot."""

    dataset: EvaluationSet
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: AwareDatetime = Field(strict=True)
    mode: RetrievalMode
    top_k: int = Field(strict=True, gt=0)
    candidate_limit: int = Field(strict=True, gt=0)
    rrf_k: int = Field(strict=True, gt=0)
    results: tuple[EvaluationCaseResult, ...]
    metrics: AggregateMetrics

    @model_validator(mode="after")
    def validate_report_identity_and_limits(self) -> Self:
        if self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        if self.dataset_hash != self.dataset.content_hash:
            raise ValueError("dataset_hash does not match the evaluation dataset")
        return self

    @property
    def degraded(self) -> bool:
        """Whether any full retrieval result reports missing eligible vectors."""

        return any(case.retrieval.degraded for case in self.results)

    @property
    def missing_vector_ids(self) -> tuple[UUID, ...]:
        """Deterministic union of warnings retained in the full retrieval results."""

        ids = {unit_id for case in self.results for unit_id in case.retrieval.missing_vector_ids}
        return tuple(sorted(ids))
