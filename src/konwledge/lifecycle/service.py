"""Storage-aware lifecycle operations: publication, status, and as-of snapshots.

The revision log stays the single source of truth. ``knowledge_units.status``
is a *cache* of the current status that is rewritten in the same transaction
as every change (publication, stale mark, refresh) so filters and exports can
read it cheaply; :meth:`LifecycleService.reconcile` rebuilds it for databases
written before U10. Historical (as-of) answers are always derived from the log.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from konwledge.domain import (
    Document,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
)
from konwledge.lifecycle.status import (
    EFFECTIVE_STATUSES,
    PUBLISHED,
    REFRESHED,
    STALE,
    Publication,
    build_timeline,
    first_published_at,
    head_at,
    is_publication,
    resolve_status,
    stale_mark_at,
)
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


class KnowledgeUnitNotFoundError(LookupError):
    """Raised when a requested knowledge unit is not stored."""


class DocumentNotFoundError(LookupError):
    """Raised when a requested document version is not stored."""


class LifecycleTransitionError(ValueError):
    """Raised when a status change is not allowed for the unit's current state."""


@dataclass(frozen=True, slots=True)
class UnitState:
    """One unit version and its resolved status at a time point."""

    unit: KnowledgeUnit
    status: KnowledgeUnitStatus
    stale_reason: str | None = None

    @property
    def effective(self) -> bool:
        return self.status in EFFECTIVE_STATUSES


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The head version of one source at a time point and its unit states."""

    source: Source
    head: Document | None
    published_at: datetime | None
    units: tuple[UnitState, ...]
    retired: tuple[UnitState, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    """Effective knowledge of the whole library at ``at`` (``None`` means now)."""

    at: datetime | None
    sources: tuple[SourceSnapshot, ...]

    @property
    def effective_units(self) -> tuple[UnitState, ...]:
        return tuple(state for snapshot in self.sources for state in snapshot.units)

    def counts(self) -> dict[str, int]:
        """Number of unit versions per status, including retired ones when loaded."""

        totals = dict.fromkeys((status.value for status in KnowledgeUnitStatus), 0)
        for snapshot in self.sources:
            for state in (*snapshot.units, *snapshot.retired):
                totals[state.status.value] += 1
        return totals


@dataclass(slots=True)
class _SourceView:
    """Everything needed to resolve unit statuses of one source at one time."""

    timeline: tuple[Publication, ...]
    head: Publication | None
    head_units: tuple[KnowledgeUnit, ...]
    stale_marks: dict[UUID, Revision] = field(default_factory=dict)

    @property
    def head_lineages(self) -> frozenset[UUID]:
        return frozenset(unit.lineage_id for unit in self.head_units)

    def state_of(self, unit: KnowledgeUnit, at: datetime | None) -> UnitState | None:
        first = first_published_at(self.timeline, unit.document_id)
        published = first is not None and (at is None or first <= at)
        mark = self.stale_marks.get(unit.lineage_id)
        status = resolve_status(
            unit,
            head=self.head,
            head_lineages=self.head_lineages,
            published=published,
            stale=mark is not None,
        )
        if status is None:
            return None
        reason = None
        if status is KnowledgeUnitStatus.STALE and mark is not None:
            raw = mark.payload.get("reason")
            reason = None if raw is None else str(raw)
        return UnitState(unit=unit, status=status, stale_reason=reason)


# ---------------------------------------------------------------------------
# Building blocks shared with the ingestion pipeline (same UnitOfWork)
# ---------------------------------------------------------------------------


def load_timeline(
    uow: UnitOfWork, source_id: UUID, *, unpublished: UUID | None = None
) -> tuple[Publication, ...]:
    """Return the publication timeline of one source (see :func:`build_timeline`)."""

    return build_timeline(
        uow.documents.list_by_source(source_id),
        uow.revisions.list_for(RevisionEntityType.SOURCE, source_id),
        unpublished=unpublished,
    )


def backfill_publications(
    uow: UnitOfWork,
    source_id: UUID,
    *,
    exclude: UUID | None,
    id_factory: IdFactory,
) -> None:
    """Write explicit publications for a source that only has an implicit timeline.

    Pre-U10 databases infer the head from ingestion order. Before the first
    explicit publication is appended, that inferred history is materialized
    (at the original ``ingested_at`` times) so it is not lost.
    """

    existing = uow.revisions.list_for(RevisionEntityType.SOURCE, source_id)
    if any(is_publication(revision) for revision in existing):
        return
    documents = [
        document for document in uow.documents.list_by_source(source_id) if document.id != exclude
    ]
    previous: UUID | None = None
    for document in documents:
        uow.revisions.add(
            _publication_revision(
                source_id=source_id,
                document=document,
                previous_document_id=previous,
                created_at=document.ingested_at,
                id_factory=id_factory,
                number=uow.revisions.next_number(RevisionEntityType.SOURCE, source_id),
                extra={"backfilled": True},
            )
        )
        previous = document.id


def publish_document(
    uow: UnitOfWork,
    document: Document,
    *,
    previous_document_id: UUID | None,
    reverted: bool,
    id_factory: IdFactory,
    created_at: datetime,
) -> Revision:
    """Append a ``published`` revision that makes ``document`` its source's head."""

    revision = _publication_revision(
        source_id=document.source_id,
        document=document,
        previous_document_id=previous_document_id,
        created_at=created_at,
        id_factory=id_factory,
        number=uow.revisions.next_number(RevisionEntityType.SOURCE, document.source_id),
        extra={"reverted": True} if reverted else {},
    )
    uow.revisions.add(revision)
    return revision


def stale_lineages(uow: UnitOfWork, lineage_ids: Sequence[UUID]) -> frozenset[UUID]:
    """Return the lineages among ``lineage_ids`` that currently carry a stale mark."""

    chains = uow.revisions.list_for_many(RevisionEntityType.KNOWLEDGE_UNIT, lineage_ids)
    return frozenset(lineage for lineage, chain in chains.items() if stale_mark_at(chain))


def materialize_statuses(uow: UnitOfWork, source_id: UUID) -> int:
    """Rewrite the cached status of every published unit of a source; return changes."""

    view = _load_view(uow, source_id, at=None)
    published = {publication.document_id for publication in view.timeline}
    changed = 0
    for document_id in published:
        for unit in uow.knowledge_units.list_by_document(document_id):
            state = view.state_of(unit, None)
            if state is not None and uow.knowledge_units.set_status(unit.id, state.status):
                changed += 1
    return changed


def _load_view(uow: UnitOfWork, source_id: UUID, *, at: datetime | None) -> _SourceView:
    timeline = load_timeline(uow, source_id)
    head = head_at(timeline, at)
    head_units: tuple[KnowledgeUnit, ...] = ()
    marks: dict[UUID, Revision] = {}
    if head is not None:
        head_units = tuple(uow.knowledge_units.list_by_document(head.document_id))
        chains = uow.revisions.list_for_many(
            RevisionEntityType.KNOWLEDGE_UNIT, [unit.lineage_id for unit in head_units]
        )
        for lineage, chain in chains.items():
            mark = stale_mark_at(chain, at)
            if mark is not None:
                marks[lineage] = mark
    return _SourceView(timeline=timeline, head=head, head_units=head_units, stale_marks=marks)


def _publication_revision(
    *,
    source_id: UUID,
    document: Document,
    previous_document_id: UUID | None,
    created_at: datetime,
    id_factory: IdFactory,
    number: int,
    extra: dict[str, Any],
) -> Revision:
    payload: dict[str, Any] = {
        "change": PUBLISHED,
        "document_id": str(document.id),
        "document_checksum": document.checksum,
        **extra,
    }
    if previous_document_id is not None:
        payload["previous_document_id"] = str(previous_document_id)
    return Revision(
        id=id_factory(),
        entity_type=RevisionEntityType.SOURCE,
        entity_id=source_id,
        revision=number,
        change_type=RevisionChangeType.CREATE if number == 1 else RevisionChangeType.UPDATE,
        created_at=created_at,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Application-facing service
# ---------------------------------------------------------------------------


class LifecycleService:
    """Query statuses as of any time point and record explicit status marks."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(
        self,
        at: datetime | None = None,
        *,
        include_retired: bool = False,
        source_id: UUID | None = None,
    ) -> KnowledgeSnapshot:
        """Return the effective knowledge of every source at ``at``.

        ``units`` lists the head version's units (``active`` or ``stale``).
        With ``include_retired`` every other unit version already published at
        ``at`` is resolved too (``superseded`` or ``deleted``). ``source_id``
        restricts the snapshot to one source.
        """

        with UnitOfWork(self._session_factory) as uow:
            sources = sorted(
                (
                    source
                    for source in uow.sources.list()
                    if source_id is None or source.id == source_id
                ),
                key=lambda source: (source.uri, str(source.id)),
            )
            snapshots: list[SourceSnapshot] = []
            for source in sources:
                view = _load_view(uow, source.id, at=at)
                if view.head is None:
                    continue
                head_document = uow.documents.get(view.head.document_id)
                units = tuple(
                    state
                    for unit in view.head_units
                    if (state := view.state_of(unit, at)) is not None
                )
                retired: tuple[UnitState, ...] = ()
                if include_retired:
                    retired = self._retired_states(uow, view, at)
                snapshots.append(
                    SourceSnapshot(
                        source=source,
                        head=head_document,
                        published_at=view.head.published_at,
                        units=units,
                        retired=retired,
                    )
                )
            return KnowledgeSnapshot(at=at, sources=tuple(snapshots))

    def unit_state(self, unit_id: UUID, at: datetime | None = None) -> UnitState | None:
        """Return one unit's status at ``at``; ``None`` if it was not published yet."""

        with UnitOfWork(self._session_factory) as uow:
            unit, document = self._get_unit(uow, unit_id)
            return _load_view(uow, document.source_id, at=at).state_of(unit, at)

    def document_time(self, document_id: UUID) -> datetime:
        """Return when a document version first became its source's head."""

        with UnitOfWork(self._session_factory) as uow:
            document = uow.documents.get(document_id)
            if document is None:
                raise DocumentNotFoundError(f"document {document_id} was not found")
            published = first_published_at(load_timeline(uow, document.source_id), document.id)
            return document.ingested_at if published is None else published

    def mark_stale(self, unit_id: UUID, *, reason: str, author: str | None = None) -> Revision:
        """Flag the current version of a unit's lineage as no longer trustworthy."""

        if not reason.strip():
            raise LifecycleTransitionError("a stale mark needs a non-empty reason")
        return self._mark(unit_id, STALE, reason=reason.strip(), author=author)

    def refresh(
        self, unit_id: UUID, *, reason: str | None = None, author: str | None = None
    ) -> Revision:
        """Clear a stale mark after the knowledge was reviewed and confirmed."""

        return self._mark(unit_id, REFRESHED, reason=reason, author=author)

    def reconcile(self) -> int:
        """Rebuild the cached status column for every source; return changed rows."""

        with UnitOfWork(self._session_factory) as uow:
            changed = sum(materialize_statuses(uow, source.id) for source in uow.sources.list())
            uow.commit()
            return changed

    def _mark(
        self, unit_id: UUID, change: str, *, reason: str | None, author: str | None
    ) -> Revision:
        with UnitOfWork(self._session_factory) as uow:
            unit, document = self._get_unit(uow, unit_id)
            state = _load_view(uow, document.source_id, at=None).state_of(unit, None)
            if state is None or not state.effective:
                current = "unpublished" if state is None else state.status.value
                raise LifecycleTransitionError(
                    f"knowledge unit {unit_id} is {current}; only the current version of a "
                    "lineage can be marked"
                )
            wanted = KnowledgeUnitStatus.STALE if change == STALE else KnowledgeUnitStatus.ACTIVE
            if state.status is wanted:
                raise LifecycleTransitionError(
                    f"knowledge unit {unit_id} is already {wanted.value}"
                )
            payload: dict[str, Any] = {
                "change": change,
                "document_id": str(document.id),
                "unit_id": str(unit.id),
                "content_hash": unit.content_hash,
            }
            if reason:
                payload["reason"] = reason
            revision = Revision(
                id=self._id_factory(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=unit.lineage_id,
                revision=uow.revisions.next_number(
                    RevisionEntityType.KNOWLEDGE_UNIT, unit.lineage_id
                ),
                change_type=RevisionChangeType.UPDATE,
                created_at=self._clock(),
                author=author,
                payload=payload,
            )
            uow.revisions.add(revision)
            materialize_statuses(uow, document.source_id)
            uow.commit()
            return revision

    @staticmethod
    def _get_unit(uow: UnitOfWork, unit_id: UUID) -> tuple[KnowledgeUnit, Document]:
        unit = uow.knowledge_units.get(unit_id)
        if unit is None:
            raise KnowledgeUnitNotFoundError(f"knowledge unit {unit_id} was not found")
        document = uow.documents.get(unit.document_id)
        if document is None:
            raise RuntimeError(f"knowledge unit {unit.id} references a missing document")
        return unit, document

    @staticmethod
    def _retired_states(
        uow: UnitOfWork, view: _SourceView, at: datetime | None
    ) -> tuple[UnitState, ...]:
        head_id = None if view.head is None else view.head.document_id
        seen: set[UUID] = set()
        states: list[UnitState] = []
        for publication in view.timeline:
            document_id = publication.document_id
            if document_id == head_id or document_id in seen:
                continue
            seen.add(document_id)
            for unit in uow.knowledge_units.list_by_document(document_id):
                state = view.state_of(unit, at)
                if state is not None:
                    states.append(state)
        return tuple(states)
