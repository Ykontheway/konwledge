"""Read-only retrieval over one metadata snapshot and stored eligible vectors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from konwledge.domain import Document, KnowledgeUnitStatus, Source
from konwledge.domain.models import KonwledgeModel
from konwledge.indexing import IndexingService, InMemoryVectorIndex, VectorRecord
from konwledge.lifecycle.service import UnitState, _load_view
from konwledge.retrieval.fusion import RankedUnit, fuse_rankings
from konwledge.retrieval.models import RetrievalHit, RetrievalMode, RetrievalResult
from konwledge.storage import SessionFactory, UnitOfWork
from konwledge.storage.full_text import FullTextRepository


class RetrievalConfigurationError(ValueError):
    """A vector route was requested without a configured indexing service."""


class _SearchRequest(KonwledgeModel):
    """Strict call validation while allowing public enum string normalization."""

    query: str = Field(strict=True, min_length=1)
    mode: RetrievalMode
    top_k: int = Field(strict=True, gt=0)
    candidate_limit: int | None = Field(default=None, strict=True, gt=0)
    rrf_k: int = Field(strict=True, gt=0)
    evaluated_at: AwareDatetime = Field(strict=True)
    include_stale: bool = Field(strict=True)
    source_id: UUID | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> Self:
        if self.candidate_limit is not None and self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        return self


@dataclass(frozen=True, slots=True)
class _Evidence:
    source: Source
    document: Document
    state: UnitState


class RetrievalService:
    """Search only each source's eligible as-of head units.

    The metadata/revision log is authoritative, never the vector payload. For a
    small local corpus we fetch all eligible records and search a temporary exact
    cosine index, ensuring status/source filtering happens *before* route limits.
    No corpus embedding, vector repair, status reconciliation, or commit occurs.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        indexing_service: IndexingService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._indexing_service = indexing_service
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    @property
    def indexing_service(self) -> IndexingService | None:
        """Optional vector configuration, also exposed for CLI diagnostics."""

        return self._indexing_service

    def search(
        self,
        query: str,
        *,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
        include_stale: bool = False,
        source_id: UUID | None = None,
    ) -> RetrievalResult:
        request = _SearchRequest(
            query=query,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            evaluated_at=at if at is not None else self._clock(),
            include_stale=include_stale,
            source_id=source_id,
        )
        limit = (
            max(50, request.top_k) if request.candidate_limit is None else request.candidate_limit
        )
        if request.mode is not RetrievalMode.LEXICAL and self._indexing_service is None:
            raise RetrievalConfigurationError(
                f"{request.mode.value} retrieval requires an indexing_service"
            )

        with UnitOfWork(self._session_factory) as uow:
            evidence = self._load_evidence(uow, request)
            lexical: tuple[RankedUnit, ...] = ()
            vector: tuple[RankedUnit, ...] = ()
            missing: tuple[UUID, ...] = ()
            if request.mode in (RetrievalMode.LEXICAL, RetrievalMode.HYBRID):
                lexical = tuple(
                    RankedUnit(hit.unit_id, hit.score)
                    for hit in FullTextRepository(uow.session).search(
                        request.query, eligible_ids=evidence.keys(), limit=limit
                    )
                    if hit.unit_id in evidence
                )
            if request.mode in (RetrievalMode.VECTOR, RetrievalMode.HYBRID):
                vector, missing = self._vector_ranking(request.query, evidence, limit)
            hits = []
            for fused in fuse_rankings(lexical, vector, rrf_k=request.rrf_k)[: request.top_k]:
                item = evidence[fused.unit_id]
                hits.append(
                    RetrievalHit(
                        unit=item.state.unit,
                        document=item.document,
                        source=item.source,
                        status=item.state.status,
                        stale_reason=item.state.stale_reason,
                        score=fused.score,
                        lexical_rank=fused.lexical_rank,
                        vector_rank=fused.vector_rank,
                        lexical_score=fused.lexical_score,
                        vector_score=fused.vector_score,
                    )
                )
            return RetrievalResult(
                query=request.query,
                mode=request.mode,
                evaluated_at=request.evaluated_at,
                include_stale=request.include_stale,
                top_k=request.top_k,
                candidate_limit=limit,
                rrf_k=request.rrf_k,
                hits=tuple(hits),
                missing_vector_ids=missing,
            )

    @staticmethod
    def _load_evidence(uow: UnitOfWork, request: _SearchRequest) -> dict[UUID, _Evidence]:
        evidence: dict[UUID, _Evidence] = {}
        for source in sorted(uow.sources.list(), key=lambda source: source.id):
            if request.source_id is not None and source.id != request.source_id:
                continue
            view = _load_view(uow, source.id, at=request.evaluated_at)
            if view.head is None:
                continue
            document = uow.documents.get(view.head.document_id)
            if document is None:
                raise RuntimeError(f"source {source.id} has a missing head document")
            for unit in view.head_units:
                state = view.state_of(unit, request.evaluated_at)
                if state is None:
                    continue
                if state.status is KnowledgeUnitStatus.ACTIVE or (
                    request.include_stale and state.status is KnowledgeUnitStatus.STALE
                ):
                    evidence[unit.id] = _Evidence(source, document, state)
        return evidence

    def _vector_ranking(
        self, query: str, evidence: Mapping[UUID, _Evidence], limit: int
    ) -> tuple[tuple[RankedUnit, ...], tuple[UUID, ...]]:
        indexing = self._indexing_service
        if indexing is None:
            raise RetrievalConfigurationError("vector retrieval requires an indexing_service")
        if not evidence:
            return (), ()
        index = indexing.vector_index
        eligible_ids = sorted(evidence)
        stored = index.get([str(unit_id) for unit_id in eligible_ids])
        records: list[VectorRecord] = []
        missing: list[UUID] = []
        for unit_id in eligible_ids:
            record = stored.get(str(unit_id))
            unit = evidence[unit_id].state.unit
            if (
                record is None
                or record.id != str(unit_id)
                or (
                    "content_hash" in record.payload
                    and record.payload["content_hash"] != unit.content_hash
                )
            ):
                missing.append(unit_id)
                continue
            # Discard all payload data: even an active vector can be stale or retired.
            records.append(VectorRecord(id=str(unit_id), vector=record.vector))
        temporary = InMemoryVectorIndex(dimension=index.dimension)
        temporary.upsert(records)
        # Only the query is embedded. Provider/index exceptions must propagate.
        [query_vector] = indexing.embedding_service.embed([query])
        ranking = tuple(
            RankedUnit(UUID(hit.id), hit.score)
            for hit in temporary.search(query_vector, top_k=limit)
        )
        return ranking, tuple(missing)
