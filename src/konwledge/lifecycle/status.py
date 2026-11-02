"""Pure rules for knowledge status and as-of ("time travel") resolution.

Every document version is an immutable snapshot, so "which knowledge was valid
at time T?" reduces to two questions:

1. Which document version was the *head* of each source at T? The answer comes
   from the source's **publication timeline**: one ``published`` revision per
   head change, including returns to an older version (a revert).
2. Was a lineage explicitly marked ``stale`` after its last content change?
   The answer comes from the lineage's own revision chain.

For one stored unit version and a time point T the status is:

=============  ==============================================================
``None``       its document version had not been published yet at T
``active``     its document is the head at T and the lineage is not stale
``stale``      its document is the head at T and the newest lineage revision
               at or before T is a ``stale`` mark
``superseded`` its document is no longer the head, but the lineage lives on
               in the head version (modified, moved, or carried over)
``deleted``    its document is no longer the head and the head has no unit of
               this lineage
=============  ==============================================================

Nothing here touches storage, which keeps the rules easy to test exhaustively.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from konwledge.domain import (
    Document,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionEntityType,
)

PUBLISHED = "published"
STALE = "stale"
REFRESHED = "refreshed"

EFFECTIVE_STATUSES = frozenset({KnowledgeUnitStatus.ACTIVE, KnowledgeUnitStatus.STALE})


@dataclass(frozen=True, slots=True)
class Publication:
    """A moment from which ``document_id`` was the head version of its source."""

    document_id: UUID
    published_at: datetime
    reverted: bool = False


def is_publication(revision: Revision) -> bool:
    """Whether ``revision`` moved a source's head to a document version."""

    return (
        revision.entity_type is RevisionEntityType.SOURCE
        and revision.payload.get("change") == PUBLISHED
    )


def build_timeline(
    documents: Sequence[Document],
    source_revisions: Sequence[Revision],
    *,
    unpublished: UUID | None = None,
) -> tuple[Publication, ...]:
    """Return the ordered head changes of one source.

    Explicit ``published`` revisions are authoritative. Databases written
    before U10 have none, so their timeline falls back to every document
    version in ingestion order, which is exactly how U09 chose the previous
    version; ``unpublished`` leaves out a document that is being ingested right
    now and must not count as its own predecessor. Consecutive publications
    of the same version are collapsed.
    """

    explicit = sorted(
        (revision for revision in source_revisions if is_publication(revision)),
        key=lambda revision: revision.revision,
    )
    if explicit:
        events = [
            Publication(
                document_id=UUID(str(revision.payload["document_id"])),
                published_at=revision.created_at,
                reverted=bool(revision.payload.get("reverted", False)),
            )
            for revision in explicit
        ]
    else:
        events = [
            Publication(document_id=document.id, published_at=document.ingested_at)
            for document in sorted(documents, key=lambda item: (item.ingested_at, str(item.id)))
            if document.id != unpublished
        ]

    timeline: list[Publication] = []
    for event in events:
        if timeline and timeline[-1].document_id == event.document_id:
            continue
        timeline.append(event)
    return tuple(timeline)


def head_at(timeline: Sequence[Publication], at: datetime | None = None) -> Publication | None:
    """Return the publication in effect at ``at`` (the latest one when ``at`` is None)."""

    if at is None:
        return timeline[-1] if timeline else None
    head: Publication | None = None
    for publication in timeline:
        if publication.published_at <= at:
            head = publication
    return head


def first_published_at(timeline: Sequence[Publication], document_id: UUID) -> datetime | None:
    """Return when ``document_id`` first became a head version, if ever."""

    for publication in timeline:
        if publication.document_id == document_id:
            return publication.published_at
    return None


def stale_mark_at(
    lineage_revisions: Sequence[Revision], at: datetime | None = None
) -> Revision | None:
    """Return the ``stale`` mark in effect for a lineage at ``at``, if any.

    A mark stays in effect until a later revision of the same lineage: a
    content change, a revert, a removal, or an explicit ``refreshed`` mark.
    Re-importing unchanged content adds no revision, so it keeps the mark.
    """

    visible = [
        revision for revision in lineage_revisions if at is None or revision.created_at <= at
    ]
    if not visible:
        return None
    latest = max(visible, key=lambda revision: revision.revision)
    return latest if latest.payload.get("change") == STALE else None


def resolve_status(
    unit: KnowledgeUnit,
    *,
    head: Publication | None,
    head_lineages: Collection[UUID],
    published: bool,
    stale: bool = False,
) -> KnowledgeUnitStatus | None:
    """Apply the status table from the module docstring to one unit version."""

    if not published or head is None:
        return None
    if unit.document_id == head.document_id:
        return KnowledgeUnitStatus.STALE if stale else KnowledgeUnitStatus.ACTIVE
    if unit.lineage_id in head_lineages:
        return KnowledgeUnitStatus.SUPERSEDED
    return KnowledgeUnitStatus.DELETED
