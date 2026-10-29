"""Coordinate file ingestion with idempotent, lineage-aware unit persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from konwledge.domain import Document, KnowledgeUnit, Revision, RevisionEntityType
from konwledge.ingestion.service import FileIngestionService, IngestionResult
from konwledge.ingestion.splitting import TraceableTextSplitter
from konwledge.lifecycle import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DocumentDiff,
    diff_units,
    initial_revisions,
    revisions_for_diff,
)
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SplittingResult:
    """Persisted units, whether this call created them, and the version diff.

    ``diff`` is set only when this call created the units of a document that
    replaces an earlier version of the same source; it tells the indexing layer
    exactly which units must be re-embedded.
    """

    units: tuple[KnowledgeUnit, ...]
    created: bool
    diff: DocumentDiff | None = None
    previous_document_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TraceableIngestionResult:
    """Document ingestion result paired with persisted traceable units."""

    ingestion: IngestionResult
    units: tuple[KnowledgeUnit, ...]
    units_created: bool
    diff: DocumentDiff | None = None
    previous_document_id: UUID | None = None


class KnowledgeUnitService:
    """Split and persist units once for each immutable document version.

    When a source already has an older document version, the new units are
    diffed against it: matched units inherit the old ``lineage_id`` and every
    real change is appended to the lineage's revision chain in the same
    transaction as the units themselves.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        splitter: TraceableTextSplitter | None = None,
        revision_id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._session_factory = session_factory
        self._splitter = splitter or TraceableTextSplitter()
        self._revision_id_factory = revision_id_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._similarity_threshold = similarity_threshold

    def ensure_units(self, document: Document, content: str) -> SplittingResult:
        with UnitOfWork(self._session_factory) as uow:
            existing = tuple(uow.knowledge_units.list_by_document(document.id))
            if existing:
                return SplittingResult(units=existing, created=False)

            split = self._splitter.split(document, content)
            previous = _previous_version(uow, document)
            diff: DocumentDiff | None = None
            if previous is None:
                units = split
                revisions = initial_revisions(
                    units,
                    document=document,
                    id_factory=self._revision_id_factory,
                    clock=self._clock,
                    next_number=_revision_counter(uow),
                )
            else:
                previous_units = uow.knowledge_units.list_by_document(previous.id)
                diff = diff_units(
                    previous_units,
                    split,
                    similarity_threshold=self._similarity_threshold,
                ).with_inherited_lineage()
                units = diff.new_units
                revisions = revisions_for_diff(
                    diff,
                    document=document,
                    previous_document_id=previous.id,
                    id_factory=self._revision_id_factory,
                    clock=self._clock,
                    next_number=_revision_counter(uow),
                )
                if not units:
                    # An emptied document has no units to make the call
                    # idempotent, so skip lineages already closed by it.
                    revisions = _unrecorded(uow, revisions, document.id)

            if not units and not revisions:
                return SplittingResult(units=(), created=False)

            try:
                for unit in units:
                    uow.knowledge_units.add(unit)
                for revision in revisions:
                    uow.revisions.add(revision)
                uow.commit()
            except IntegrityError:
                uow.rollback()
                existing = tuple(uow.knowledge_units.list_by_document(document.id))
                if not existing:
                    raise
                return SplittingResult(units=existing, created=False)

            return SplittingResult(
                units=units,
                created=bool(units),
                diff=diff,
                previous_document_id=None if previous is None else previous.id,
            )


def _previous_version(uow: UnitOfWork, document: Document) -> Document | None:
    """Return the newest other version of the same source ingested before ``document``."""

    candidates = [
        candidate
        for candidate in uow.documents.list_by_source(document.source_id)
        if candidate.id != document.id and candidate.ingested_at <= document.ingested_at
    ]
    return candidates[-1] if candidates else None


def _revision_counter(uow: UnitOfWork) -> Callable[[UUID], int]:
    """Number revisions per lineage, including ones not yet flushed in this call."""

    issued: dict[UUID, int] = {}

    def next_number(lineage_id: UUID) -> int:
        if lineage_id not in issued:
            issued[lineage_id] = uow.revisions.next_number(
                RevisionEntityType.KNOWLEDGE_UNIT, lineage_id
            )
        else:
            issued[lineage_id] += 1
        return issued[lineage_id]

    return next_number


def _unrecorded(uow: UnitOfWork, revisions: list[Revision], document_id: UUID) -> list[Revision]:
    marker = str(document_id)
    pending: list[Revision] = []
    for revision in revisions:
        latest = uow.revisions.latest_for(RevisionEntityType.KNOWLEDGE_UNIT, revision.entity_id)
        if latest is not None and latest.payload.get("document_id") == marker:
            continue
        pending.append(revision)
    return pending


class TraceableIngestionService:
    """Ingest a file and ensure its normalized content has traceable units."""

    def __init__(
        self,
        ingestion_service: FileIngestionService,
        knowledge_unit_service: KnowledgeUnitService,
    ) -> None:
        self._ingestion_service = ingestion_service
        self._knowledge_unit_service = knowledge_unit_service

    def ingest(self, source_id: UUID) -> TraceableIngestionResult:
        ingestion = self._ingestion_service.ingest(source_id)
        splitting = self._knowledge_unit_service.ensure_units(ingestion.document, ingestion.content)
        return TraceableIngestionResult(
            ingestion=ingestion,
            units=splitting.units,
            units_created=splitting.created,
            diff=splitting.diff,
            previous_document_id=splitting.previous_document_id,
        )
