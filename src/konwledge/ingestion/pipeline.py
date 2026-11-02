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
    backfill_publications,
    diff_by_lineage,
    diff_units,
    head_at,
    initial_revisions,
    load_timeline,
    materialize_statuses,
    publish_document,
    revisions_for_diff,
)
from konwledge.lifecycle.status import first_published_at
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SplittingResult:
    """Persisted units, whether this call created them, and the version diff.

    ``diff`` is set whenever this call changed the head version of a source
    that already had one: either a brand-new document version or a return to
    an older one (``reverted``). It tells the indexing layer exactly which
    units must be re-embedded.
    """

    units: tuple[KnowledgeUnit, ...]
    created: bool
    diff: DocumentDiff | None = None
    previous_document_id: UUID | None = None
    reverted: bool = False


@dataclass(frozen=True, slots=True)
class TraceableIngestionResult:
    """Document ingestion result paired with persisted traceable units."""

    ingestion: IngestionResult
    units: tuple[KnowledgeUnit, ...]
    units_created: bool
    diff: DocumentDiff | None = None
    previous_document_id: UUID | None = None
    reverted: bool = False


class KnowledgeUnitService:
    """Split, persist, and publish each immutable document version.

    Every call ends with the ingested document as its source's *head* version:

    * a new version is split and diffed against the current head, so matched
      units inherit the old ``lineage_id``;
    * a version that was published before (a revert to older content) reuses
      its stored units and is diffed against the head by lineage;
    * the head itself is a no-op, which keeps re-imports idempotent.

    Unit revisions, the source ``published`` revision, and the cached unit
    statuses are written in one transaction.
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
            # Pre-U10 databases only have an implicit timeline; materialize it
            # first, leaving out a document that has not been split yet.
            unsplit = None if existing else document.id
            backfill_publications(
                uow,
                document.source_id,
                exclude=unsplit,
                id_factory=self._revision_id_factory,
            )
            timeline = load_timeline(uow, document.source_id, unpublished=unsplit)
            head = head_at(timeline)
            if head is not None and head.document_id == document.id:
                uow.commit()  # keep a backfill, if one was needed
                return SplittingResult(units=existing, created=False)

            now = self._clock()
            previous_id = None if head is None else head.document_id
            previous_units = (
                () if previous_id is None else uow.knowledge_units.list_by_document(previous_id)
            )
            revisit = bool(existing) or first_published_at(timeline, document.id) is not None
            diff: DocumentDiff | None = None
            units: tuple[KnowledgeUnit, ...] = ()
            if revisit:
                diff = diff_by_lineage(previous_units, existing)
                revisions = revisions_for_diff(
                    diff,
                    document=document,
                    previous_document_id=previous_id or document.id,
                    id_factory=self._revision_id_factory,
                    clock=lambda: now,
                    next_number=_revision_counter(uow),
                    extra_payload={"reverted": True},
                )
            elif previous_id is None:
                units = self._splitter.split(document, content)
                revisions = initial_revisions(
                    units,
                    document=document,
                    id_factory=self._revision_id_factory,
                    clock=lambda: now,
                    next_number=_revision_counter(uow),
                )
            else:
                diff = diff_units(
                    previous_units,
                    self._splitter.split(document, content),
                    similarity_threshold=self._similarity_threshold,
                ).with_inherited_lineage()
                units = diff.new_units
                revisions = revisions_for_diff(
                    diff,
                    document=document,
                    previous_document_id=previous_id,
                    id_factory=self._revision_id_factory,
                    clock=lambda: now,
                    next_number=_revision_counter(uow),
                )
                if not units:
                    # Legacy databases may already hold the deletes of an
                    # emptied document; do not record them twice.
                    revisions = _unrecorded(uow, revisions, document.id)

            try:
                for unit in units:
                    uow.knowledge_units.add(unit)
                for revision in revisions:
                    uow.revisions.add(revision)
                publish_document(
                    uow,
                    document,
                    previous_document_id=previous_id,
                    reverted=revisit,
                    id_factory=self._revision_id_factory,
                    created_at=now,
                )
                materialize_statuses(uow, document.source_id)
                persisted = tuple(uow.knowledge_units.list_by_document(document.id))
                uow.commit()
            except IntegrityError:
                uow.rollback()
                existing = tuple(uow.knowledge_units.list_by_document(document.id))
                if not existing:
                    raise
                return SplittingResult(units=existing, created=False)

            return SplittingResult(
                units=persisted,
                created=bool(units),
                diff=None if diff is None else diff.with_new_units(persisted),
                previous_document_id=previous_id,
                reverted=revisit,
            )


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
            reverted=splitting.reverted,
        )
