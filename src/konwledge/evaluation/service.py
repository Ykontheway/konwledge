"""Immutable dataset imports and read-only retrieval evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.exc import IntegrityError

from konwledge.domain import EvaluationSet
from konwledge.domain.models import KonwledgeModel
from konwledge.evaluation.models import EvaluationCaseResult, EvaluationReport, ItemMetrics
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage import ImmutableEntityError, SessionFactory, UnitOfWork

Clock = Callable[[], datetime]


class EvaluationSetNotFoundError(LookupError):
    """Raised when a requested evaluation dataset or version does not exist."""


@dataclass(frozen=True, slots=True)
class EvaluationImportResult:
    """The imported snapshot and whether this call created it."""

    dataset: EvaluationSet
    created: bool


class _RunRequest(KonwledgeModel):
    mode: RetrievalMode
    top_k: int = Field(strict=True, gt=0)
    candidate_limit: int | None = Field(default=None, strict=True, gt=0)
    rrf_k: int = Field(strict=True, gt=0)
    evaluated_at: AwareDatetime = Field(strict=True)

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> Self:
        if self.candidate_limit is not None and self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        return self


class EvaluationService:
    """Version evaluation datasets and evaluate retrieval without mutating the corpus."""

    def __init__(
        self,
        session_factory: SessionFactory,
        retrieval_service: RetrievalService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._retrieval_service = retrieval_service
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def import_set(self, dataset: EvaluationSet) -> EvaluationImportResult:
        """Validate and atomically append one complete dataset version.

        Gold lineages may refer to inactive, superseded, or deleted historical units,
        because evaluation history must remain reproducible. A case's optional
        ``source_id`` must exist, including for no-answer cases; unknown sources are
        rejected rather than silently turning a scoped query into a no-hit result.
        """

        with UnitOfWork(self._session_factory) as uow:
            existing = uow.evaluation_sets.get(dataset.id, dataset.version)
            if existing is not None:
                if existing.content_hash == dataset.content_hash:
                    return EvaluationImportResult(existing, created=False)
                raise ImmutableEntityError(
                    f"evaluation set {dataset.id} version {dataset.version} is immutable"
                )

            history = uow.evaluation_sets.list_versions(dataset.id)
            self._validate_next_version(dataset, history)
            self._validate_library_references(dataset, uow)
            try:
                uow.evaluation_sets.add(dataset)
                uow.commit()
            except IntegrityError:
                uow.rollback()
                existing = uow.evaluation_sets.get(dataset.id, dataset.version)
                if existing is None:
                    raise
                if existing.content_hash == dataset.content_hash:
                    return EvaluationImportResult(existing, created=False)
                raise ImmutableEntityError(
                    f"evaluation set {dataset.id} version {dataset.version} is immutable"
                ) from None
        return EvaluationImportResult(dataset, created=True)

    def get(self, set_id: UUID, version: int | None = None) -> EvaluationSet:
        """Return one version, or the latest version when omitted."""

        with UnitOfWork(self._session_factory) as uow:
            dataset = uow.evaluation_sets.get(set_id, version)
        if dataset is None:
            suffix = "latest version" if version is None else f"version {version}"
            raise EvaluationSetNotFoundError(f"evaluation set {set_id} {suffix} was not found")
        return dataset

    def list(self) -> tuple[EvaluationSet, ...]:
        """Return the latest version of each dataset in deterministic order."""

        with UnitOfWork(self._session_factory) as uow:
            return tuple(uow.evaluation_sets.list())

    def history(self, set_id: UUID) -> tuple[EvaluationSet, ...]:
        """Return all versions in ascending order."""

        with UnitOfWork(self._session_factory) as uow:
            datasets = tuple(uow.evaluation_sets.list_versions(set_id))
        if not datasets:
            raise EvaluationSetNotFoundError(f"evaluation set {set_id} was not found")
        return datasets

    def run(
        self,
        set_id: UUID,
        *,
        version: int | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
    ) -> EvaluationReport:
        """Retrieve exactly once per case, score ranked lineages, and return a report."""

        request = _RunRequest(
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            evaluated_at=at if at is not None else self._clock(),
        )
        dataset = self.get(set_id, version)

        # Import scoring locally to keep module initialization independent.
        from konwledge.evaluation.metrics import aggregate_metrics, score_ranked_lineages

        results: list[EvaluationCaseResult] = []
        scores: list[ItemMetrics] = []
        for item in dataset.items:
            retrieval = self._retrieval_service.search(
                item.query,
                mode=request.mode,
                top_k=request.top_k,
                candidate_limit=request.candidate_limit,
                rrf_k=request.rrf_k,
                at=item.at if item.at is not None else request.evaluated_at,
                include_stale=item.include_stale,
                source_id=item.source_id,
            )
            score = score_ranked_lineages(
                item.expected_lineage_ids,
                tuple(hit.unit.lineage_id for hit in retrieval.hits),
                k=request.top_k,
                expect_no_answer=item.expect_no_answer,
            )
            scores.append(score)
            results.append(
                EvaluationCaseResult(item_id=item.id, retrieval=retrieval, metrics=score)
            )

        limit = (
            max(50, request.top_k) if request.candidate_limit is None else request.candidate_limit
        )
        return EvaluationReport(
            dataset=dataset,
            dataset_hash=dataset.content_hash,
            evaluated_at=request.evaluated_at,
            mode=request.mode,
            top_k=request.top_k,
            candidate_limit=limit,
            rrf_k=request.rrf_k,
            results=tuple(results),
            metrics=aggregate_metrics(scores),
        )

    @staticmethod
    def _validate_next_version(dataset: EvaluationSet, history: Sequence[EvaluationSet]) -> None:
        if not history:
            if dataset.version != 1:
                raise ValueError("the first evaluation set version must be 1")
            return
        latest = history[-1]
        if dataset.version != latest.version + 1:
            raise ValueError(
                f"evaluation set version must be contiguous; expected {latest.version + 1}"
            )
        if dataset.created_at < latest.created_at:
            raise ValueError("evaluation set created_at must not regress")

    @staticmethod
    def _validate_library_references(dataset: EvaluationSet, uow: UnitOfWork) -> None:
        lineages = {
            lineage_id for item in dataset.items for lineage_id in item.expected_lineage_ids
        }
        unknown = sorted(
            (
                lineage_id
                for lineage_id in lineages
                if not uow.knowledge_units.list_by_lineage(lineage_id)
            ),
            key=str,
        )
        if unknown:
            rendered = ", ".join(str(lineage_id) for lineage_id in unknown)
            raise ValueError(f"unknown expected lineage IDs: {rendered}")
        source_ids = {item.source_id for item in dataset.items if item.source_id is not None}
        missing_sources = sorted(
            (source_id for source_id in source_ids if uow.sources.get(source_id) is None),
            key=str,
        )
        if missing_sources:
            rendered = ", ".join(str(source_id) for source_id in missing_sources)
            raise ValueError(f"unknown evaluation source IDs: {rendered}")
