"""Export a knowledge library into a deterministic :class:`LibrarySnapshot`.

Everything is derived from the metadata store: statuses and as-of views come
from :class:`~konwledge.lifecycle.LifecycleService`, diffs are recomputed by
lineage between consecutive head versions, and document text is rebuilt from
the stored units (and only kept when it reproduces the stored checksum).

Determinism rules: every list has an explicit sort key, timestamps keep the
offset they were recorded with, and :func:`dump_snapshot` always produces the
same bytes for the same snapshot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

from konwledge.domain import (
    Document,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.ingestion import fingerprint_text
from konwledge.lifecycle import (
    EFFECTIVE_STATUSES,
    DocumentDiff,
    KnowledgeSnapshot,
    LifecycleService,
    Publication,
    UnitChange,
    UnitChangeKind,
    UnitState,
    diff_by_lineage,
    load_timeline,
)
from konwledge.snapshot.models import (
    DiffView,
    DocumentView,
    EmbeddingWork,
    EventView,
    LibrarySnapshot,
    LineageView,
    PublicationView,
    RevisionView,
    SnapshotStats,
    SourceView,
    StoryStep,
    TimepointView,
    UnitChangeView,
    UnitView,
    snapshot_json_schema,
)
from konwledge.storage import SessionFactory, UnitOfWork


def export_snapshot(
    session_factory: SessionFactory,
    *,
    title: str = "Knowledge CI",
    root: Path | None = None,
    story: Sequence[StoryStep] = (),
) -> LibrarySnapshot:
    """Return the snapshot of every source in the library.

    ``root`` makes file source URIs relative (default: the deepest directory
    shared by all file sources), so snapshots do not leak machine paths.
    """

    lifecycle = LifecycleService(session_factory)
    current = lifecycle.snapshot(include_retired=True)
    states = _states_by_unit(current)

    with UnitOfWork(session_factory) as uow:
        sources = sorted(uow.sources.list(), key=lambda source: (source.uri, str(source.id)))
        base = root if root is not None else _common_root(sources)
        source_views: list[SourceView] = []
        lineage_views: list[LineageView] = []
        events: list[EventView] = []
        for source in sources:
            view, lineages, source_events = _export_source(uow, source, states, base)
            source_views.append(view)
            lineage_views.extend(lineages)
            events.extend(source_events)
        revision_total = len(uow.revisions.list())

    events.sort(key=lambda event: (event.at, _EVENT_ORDER[event.kind], str(event.source_id)))
    timepoints = [
        _timepoint(lifecycle.snapshot(moment, include_retired=True), moment)
        for moment in sorted({event.at for event in events})
    ]
    return LibrarySnapshot(
        title=title,
        as_of=events[-1].at if events else None,
        stats=_stats(source_views, lineage_views, current, revision_total),
        sources=source_views,
        lineages=lineage_views,
        events=events,
        timepoints=timepoints,
        story=list(story),
    )


def dump_snapshot(snapshot: LibrarySnapshot) -> str:
    """Serialize ``snapshot`` to canonical JSON text (UTF-8, LF, trailing newline)."""

    return _dump(snapshot.model_dump(mode="json"))


def dump_schema() -> str:
    """Serialize the snapshot JSON Schema in the same canonical format."""

    return _dump(snapshot_json_schema())


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Sources, documents, and diffs
# ---------------------------------------------------------------------------


def _export_source(
    uow: UnitOfWork,
    source: Source,
    states: Mapping[UUID, UnitState],
    base: Path | None,
) -> tuple[SourceView, list[LineageView], list[EventView]]:
    timeline = load_timeline(uow, source.id)
    versions = _version_numbers(timeline)
    head_id = timeline[-1].document_id if timeline else None
    documents = sorted(
        uow.documents.list_by_source(source.id),
        key=lambda document: (
            versions.get(document.id, len(versions) + 1),
            document.ingested_at,
            str(document.id),
        ),
    )
    units = {
        document.id: tuple(
            sorted(uow.knowledge_units.list_by_document(document.id), key=lambda u: u.ordinal)
        )
        for document in documents
    }
    first_published: dict[UUID, datetime] = {}
    for publication in timeline:
        first_published.setdefault(publication.document_id, publication.published_at)

    document_views = [
        _document_view(
            document,
            units[document.id],
            states,
            version=versions.get(document.id),
            published_at=first_published.get(document.id),
            is_head=document.id == head_id,
        )
        for document in documents
    ]
    diffs = [
        _diff_view(previous, publication, units, versions)
        for previous, publication in pairwise(timeline)
    ]
    lineages = _lineage_views(uow, source, documents, units, states)
    events = [
        EventView(
            at=publication.published_at,
            kind="reverted" if publication.reverted else "published",
            source_id=source.id,
            document_id=publication.document_id,
            version=versions[publication.document_id],
        )
        for publication in timeline
    ]
    for lineage in lineages:
        for revision in lineage.revisions:
            if revision.change in {"stale", "refreshed"}:
                events.append(
                    EventView(
                        at=revision.created_at,
                        kind="stale" if revision.change == "stale" else "refreshed",
                        source_id=source.id,
                        document_id=revision.document_id,
                        version=None
                        if revision.document_id is None
                        else versions.get(revision.document_id),
                        lineage_id=lineage.id,
                        unit_id=revision.unit_id,
                        reason=revision.reason,
                    )
                )
    view = SourceView(
        id=source.id,
        kind=source.kind,
        uri=_relative_uri(source, base),
        title=source.title,
        head_document_id=head_id,
        documents=document_views,
        timeline=[
            PublicationView(
                document_id=publication.document_id,
                version=versions[publication.document_id],
                published_at=publication.published_at,
                reverted=publication.reverted,
            )
            for publication in timeline
        ],
        diffs=diffs,
    )
    return view, lineages, events


def _version_numbers(timeline: Sequence[Publication]) -> dict[UUID, int]:
    versions: dict[UUID, int] = {}
    for publication in timeline:
        versions.setdefault(publication.document_id, len(versions) + 1)
    return versions


def _document_view(
    document: Document,
    units: Sequence[KnowledgeUnit],
    states: Mapping[UUID, UnitState],
    *,
    version: int | None,
    published_at: datetime | None,
    is_head: bool,
) -> DocumentView:
    rebuilt = "".join(unit.text for unit in units)
    content = rebuilt if units and fingerprint_text(rebuilt) == document.checksum else None
    return DocumentView(
        id=document.id,
        version=version,
        checksum=document.checksum,
        content_type=document.content_type,
        ingested_at=document.ingested_at,
        published_at=published_at,
        is_head=is_head,
        content=content,
        units=[_unit_view(unit, states.get(unit.id)) for unit in units],
    )


def _unit_view(unit: KnowledgeUnit, state: UnitState | None) -> UnitView:
    return UnitView(
        id=unit.id,
        lineage_id=unit.lineage_id,
        ordinal=unit.ordinal,
        start_char=unit.start_char,
        end_char=unit.end_char,
        heading=unit.heading,
        section_path=list(unit.section_path),
        text=unit.text,
        content_hash=unit.content_hash,
        status=None if state is None else state.status,
        stale_reason=None if state is None else state.stale_reason,
    )


def _diff_view(
    previous: Publication,
    publication: Publication,
    units: Mapping[UUID, Sequence[KnowledgeUnit]],
    versions: Mapping[UUID, int],
) -> DiffView:
    diff = diff_by_lineage(units[previous.document_id], units[publication.document_id])
    return DiffView(
        from_document_id=previous.document_id,
        to_document_id=publication.document_id,
        from_version=versions[previous.document_id],
        to_version=versions[publication.document_id],
        published_at=publication.published_at,
        reverted=publication.reverted,
        summary=diff.summary(),
        counts=diff.counts(),
        embedding=embedding_work(diff),
        changes=[
            UnitChangeView(
                kind=change.kind,
                lineage_id=_change_lineage(change),
                old_unit_id=None if change.old is None else change.old.id,
                new_unit_id=None if change.new is None else change.new.id,
                similarity=change.similarity,
                needs_embedding=change.needs_embedding,
            )
            for change in diff.changes
        ],
    )


def _change_lineage(change: UnitChange) -> UUID:
    subject = change.new if change.new is not None else change.old
    if subject is None:  # pragma: no cover - a change always carries one side
        raise ValueError("a unit change must reference at least one unit")
    return subject.lineage_id


def embedding_work(diff: DocumentDiff) -> EmbeddingWork:
    """Vector work of :meth:`IndexingService.apply_diff` for ``diff``.

    Mirrors the indexing rules: added/modified units are embedded, the other
    new units reuse the vector of their old unit, and every old unit id that
    is not part of the new version is deleted.
    """

    new_ids = {unit.id for unit in diff.new_units}
    embedded = len(diff.affected)
    return EmbeddingWork(
        embedded=embedded,
        reused=len(new_ids) - embedded,
        deleted=sum(
            1 for change in diff.changes if change.old is not None and change.old.id not in new_ids
        ),
    )


# ---------------------------------------------------------------------------
# Lineages
# ---------------------------------------------------------------------------


def _lineage_views(
    uow: UnitOfWork,
    source: Source,
    documents: Sequence[Document],
    units: Mapping[UUID, Sequence[KnowledgeUnit]],
    states: Mapping[UUID, UnitState],
) -> list[LineageView]:
    by_lineage: dict[UUID, list[KnowledgeUnit]] = {}
    first_seen: dict[UUID, tuple[int, int]] = {}
    for document_index, document in enumerate(documents):
        for unit in units[document.id]:
            by_lineage.setdefault(unit.lineage_id, []).append(unit)
            first_seen.setdefault(unit.lineage_id, (document_index, unit.ordinal))
    chains = uow.revisions.list_for_many(RevisionEntityType.KNOWLEDGE_UNIT, list(by_lineage))
    ordered = sorted(by_lineage, key=lambda lineage: (first_seen[lineage], str(lineage)))
    views: list[LineageView] = []
    for lineage in ordered:
        members = by_lineage[lineage]  # already in document (publication) order
        views.append(
            LineageView(
                id=lineage,
                source_id=source.id,
                label=_label(members[-1]),
                status=_lineage_status([states.get(unit.id) for unit in members]),
                unit_ids=[unit.id for unit in members],
                revisions=[_revision_view(revision) for revision in chains.get(lineage, ())],
            )
        )
    return views


def _label(unit: KnowledgeUnit) -> str:
    if unit.heading:
        return unit.heading
    line = " ".join(unit.text.split())
    return line if len(line) <= 40 else line[:39] + "…"


def _lineage_status(states: Iterable[UnitState | None]) -> KnowledgeUnitStatus | None:
    resolved = [state.status for state in states if state is not None]
    for status in resolved:
        if status in EFFECTIVE_STATUSES:
            return status
    if KnowledgeUnitStatus.DELETED in resolved:
        return KnowledgeUnitStatus.DELETED
    return resolved[-1] if resolved else None


def _revision_view(revision: Revision) -> RevisionView:
    payload = revision.payload
    similarity = payload.get("similarity")
    reason = payload.get("reason")
    return RevisionView(
        revision=revision.revision,
        change_type=revision.change_type.value,
        change=str(payload.get("change", revision.change_type.value)),
        created_at=revision.created_at,
        document_id=_uuid(payload.get("document_id")),
        unit_id=_uuid(payload.get("unit_id")),
        previous_unit_id=_uuid(payload.get("previous_unit_id")),
        similarity=None if similarity is None else float(similarity),
        reason=None if reason is None else str(reason),
        reverted=bool(payload.get("reverted", False)),
    )


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


# ---------------------------------------------------------------------------
# Time points, stats, and helpers
# ---------------------------------------------------------------------------

_EVENT_ORDER = {"published": 0, "reverted": 0, "stale": 1, "refreshed": 1}


def _states_by_unit(snapshot: KnowledgeSnapshot) -> dict[UUID, UnitState]:
    return {
        state.unit.id: state
        for source in snapshot.sources
        for state in (*source.units, *source.retired)
    }


def _timepoint(snapshot: KnowledgeSnapshot, moment: datetime) -> TimepointView:
    states = _states_by_unit(snapshot)
    return TimepointView(
        at=moment,
        head_document_ids=[source.head.id for source in snapshot.sources if source.head],
        effective_unit_ids=[state.unit.id for state in snapshot.effective_units],
        counts=snapshot.counts(),
        statuses={str(unit_id): states[unit_id].status for unit_id in sorted(states, key=str)},
    )


def _stats(
    sources: Sequence[SourceView],
    lineages: Sequence[LineageView],
    current: KnowledgeSnapshot,
    revisions: int,
) -> SnapshotStats:
    documents = [document for source in sources for document in source.documents]
    diff_kinds = dict.fromkeys((kind.value for kind in UnitChangeKind), 0)
    for source in sources:
        for diff in source.diffs:
            for kind, count in diff.counts.items():
                diff_kinds[kind] += count
    return SnapshotStats(
        sources=len(sources),
        documents=len(documents),
        published_documents=sum(1 for document in documents if document.version is not None),
        units=sum(len(document.units) for document in documents),
        lineages=len(lineages),
        revisions=revisions,
        vectors=len(current.effective_units),
        statuses=current.counts(),
        diff_kinds=diff_kinds,
    )


def _common_root(sources: Sequence[Source]) -> Path | None:
    parents = [str(Path(source.uri).parent) for source in sources if source.kind is SourceKind.FILE]
    if not parents:
        return None
    return Path(os.path.commonpath(parents))


def _relative_uri(source: Source, base: Path | None) -> str:
    if source.kind is not SourceKind.FILE or base is None:
        return source.uri
    try:
        return Path(source.uri).relative_to(base).as_posix()
    except ValueError:
        return Path(source.uri).name
