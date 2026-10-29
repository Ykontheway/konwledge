"""Turn a unit-level diff into append-only revision records.

Revisions are keyed by ``lineage_id`` rather than by unit id: every document
version creates new immutable units, so the lineage is the only identity that
survives edits. One lineage therefore reads like a commit log::

    rev 1  create   (first import)
    rev 2  update   (text modified)
    rev 3  update   (moved to another section)
    rev 4  delete   (removed from the document)

Unchanged units do not produce revisions; their vectors and history carry over
untouched, which is exactly what keeps incremental updates cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from konwledge.domain import (
    Document,
    KnowledgeUnit,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
)
from konwledge.lifecycle.diff import DocumentDiff, UnitChange, UnitChangeKind

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]
NextRevisionNumber = Callable[[UUID], int]

_CHANGE_TYPES: dict[UnitChangeKind, RevisionChangeType] = {
    UnitChangeKind.ADDED: RevisionChangeType.CREATE,
    UnitChangeKind.MODIFIED: RevisionChangeType.UPDATE,
    UnitChangeKind.MOVED: RevisionChangeType.UPDATE,
    UnitChangeKind.REMOVED: RevisionChangeType.DELETE,
}


def initial_revisions(
    units: tuple[KnowledgeUnit, ...] | list[KnowledgeUnit],
    *,
    document: Document,
    id_factory: IdFactory,
    clock: Clock,
    next_number: NextRevisionNumber,
) -> list[Revision]:
    """Return one ``create`` revision per unit of a source's first document version."""

    created_at = clock()
    return [
        _revision(
            UnitChange(UnitChangeKind.ADDED, None, unit, 0.0),
            document=document,
            id_factory=id_factory,
            created_at=created_at,
            next_number=next_number,
            previous_document_id=None,
        )
        for unit in units
    ]


def revisions_for_diff(
    diff: DocumentDiff,
    *,
    document: Document,
    previous_document_id: UUID,
    id_factory: IdFactory,
    clock: Clock,
    next_number: NextRevisionNumber,
) -> list[Revision]:
    """Return the revisions a new document version adds to each affected lineage."""

    created_at = clock()
    return [
        _revision(
            change,
            document=document,
            id_factory=id_factory,
            created_at=created_at,
            next_number=next_number,
            previous_document_id=previous_document_id,
        )
        for change in diff.changes
        if change.kind is not UnitChangeKind.UNCHANGED
    ]


def _revision(
    change: UnitChange,
    *,
    document: Document,
    id_factory: IdFactory,
    created_at: datetime,
    next_number: NextRevisionNumber,
    previous_document_id: UUID | None,
) -> Revision:
    subject = change.new if change.new is not None else change.old
    if subject is None:  # pragma: no cover - UnitChange always carries one side
        raise ValueError("a unit change must reference at least one unit")
    payload: dict[str, Any] = {
        "change": change.kind.value,
        "document_id": str(document.id),
        "document_checksum": document.checksum,
    }
    if previous_document_id is not None:
        payload["previous_document_id"] = str(previous_document_id)
    if change.new is not None:
        payload.update(
            unit_id=str(change.new.id),
            ordinal=change.new.ordinal,
            section_path=list(change.new.section_path),
            content_hash=change.new.content_hash,
        )
    if change.old is not None:
        payload["previous_unit_id"] = str(change.old.id)
        payload["previous_content_hash"] = change.old.content_hash
    if change.kind is UnitChangeKind.MODIFIED:
        payload["similarity"] = change.similarity
    return Revision(
        id=id_factory(),
        entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
        entity_id=subject.lineage_id,
        revision=next_number(subject.lineage_id),
        change_type=_CHANGE_TYPES[change.kind],
        created_at=created_at,
        payload=payload,
    )
