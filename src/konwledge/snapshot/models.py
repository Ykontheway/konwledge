"""Versioned, read-only snapshot of a knowledge library for the demo site.

The snapshot is the *only* contract between the Python core and the static
front end: every number the page shows is computed here, so the page never
re-implements lifecycle rules. Bump :data:`SCHEMA_VERSION` whenever a change
is not purely additive-optional, and regenerate the committed JSON Schema.

Field order is part of the byte-level determinism of exported files, so new
fields should be appended rather than inserted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from konwledge.domain import DocumentContentType, KnowledgeUnitStatus, SourceKind
from konwledge.lifecycle import UnitChangeKind

SCHEMA_VERSION = 1
SCHEMA_ID = "https://github.com/Ykontheway/konwledge/blob/main/demo/snapshot.schema.json"

EventKind = Literal["published", "reverted", "stale", "refreshed"]


class SnapshotModel(BaseModel):
    """Strict, immutable base for every snapshot object."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class UnitView(SnapshotModel):
    """One stored knowledge unit version and its current status."""

    id: UUID
    lineage_id: UUID
    ordinal: int = Field(ge=0)
    start_char: int = Field(ge=0, description="Inclusive offset into the document content.")
    end_char: int = Field(description="Exclusive offset into the document content.")
    heading: str | None
    section_path: list[str]
    text: str
    content_hash: str
    status: KnowledgeUnitStatus | None = Field(
        description="Current status; null when the document version was never published."
    )
    stale_reason: str | None = None


class DocumentView(SnapshotModel):
    """One immutable document version of a source."""

    id: UUID
    version: int | None = Field(
        description="1-based order of first publication; null if never published."
    )
    checksum: str
    content_type: DocumentContentType
    ingested_at: datetime
    published_at: datetime | None = Field(description="When the version first became head.")
    is_head: bool
    content: str | None = Field(
        description=(
            "Normalized text rebuilt from the units and verified against the checksum; "
            "null when the units do not reproduce it exactly."
        )
    )
    units: list[UnitView]


class PublicationView(SnapshotModel):
    """A head change of one source."""

    document_id: UUID
    version: int
    published_at: datetime
    reverted: bool


class UnitChangeView(SnapshotModel):
    """One unit-level change between two consecutive head versions."""

    kind: UnitChangeKind
    lineage_id: UUID
    old_unit_id: UUID | None
    new_unit_id: UUID | None
    similarity: float = Field(ge=0.0, le=1.0)
    needs_embedding: bool


class EmbeddingWork(SnapshotModel):
    """Vector work an incremental index update performs for one diff."""

    embedded: int = Field(ge=0, description="Added and modified units that are re-embedded.")
    reused: int = Field(ge=0, description="Unchanged and moved units that keep their vector.")
    deleted: int = Field(ge=0, description="Vectors of the previous version that are removed.")


class DiffView(SnapshotModel):
    """The unit-level diff behind one publication of a source."""

    from_document_id: UUID
    to_document_id: UUID
    from_version: int
    to_version: int
    published_at: datetime
    reverted: bool
    summary: str
    counts: dict[str, int] = Field(description="Number of changes per kind, all five kinds.")
    embedding: EmbeddingWork
    changes: list[UnitChangeView]


class SourceView(SnapshotModel):
    """A source, all of its document versions, and how its head moved."""

    id: UUID
    kind: SourceKind
    uri: str = Field(description="Path relative to the export root for file sources.")
    title: str | None
    head_document_id: UUID | None
    documents: list[DocumentView]
    timeline: list[PublicationView]
    diffs: list[DiffView]


class RevisionView(SnapshotModel):
    """One entry of a lineage's append-only revision chain."""

    revision: int = Field(ge=1)
    change_type: str
    change: str
    created_at: datetime
    document_id: UUID | None
    unit_id: UUID | None
    previous_unit_id: UUID | None
    similarity: float | None
    reason: str | None
    reverted: bool


class LineageView(SnapshotModel):
    """One piece of knowledge followed across document versions."""

    id: UUID
    source_id: UUID
    label: str
    status: KnowledgeUnitStatus | None
    unit_ids: list[UUID] = Field(description="Unit versions in publication order.")
    revisions: list[RevisionView]


class EventView(SnapshotModel):
    """A moment that changed effective knowledge: a publication or a status mark."""

    at: datetime
    kind: EventKind
    source_id: UUID
    document_id: UUID | None = None
    version: int | None = None
    lineage_id: UUID | None = None
    unit_id: UUID | None = None
    reason: str | None = None


class TimepointView(SnapshotModel):
    """Statuses of every unit version published at ``at`` (the as-of view)."""

    at: datetime
    head_document_ids: list[UUID]
    effective_unit_ids: list[UUID]
    counts: dict[str, int]
    statuses: dict[str, KnowledgeUnitStatus] = Field(
        description="Status per unit id; unit versions published later are absent."
    )


class StoryStep(SnapshotModel):
    """A narrated step of a scripted scenario, aligned with an event time."""

    at: datetime
    title: str
    description: str


class SnapshotStats(SnapshotModel):
    """Headline counts for the overview page."""

    sources: int
    documents: int
    published_documents: int
    units: int
    lineages: int
    revisions: int
    vectors: int = Field(description="Units served by the index: the current head versions.")
    statuses: dict[str, int]
    diff_kinds: dict[str, int] = Field(description="Changes per kind across all diffs.")


class LibrarySnapshot(SnapshotModel):
    """Everything the demo site renders, exported from one knowledge library."""

    schema_version: Literal[1] = 1  # keep in sync with SCHEMA_VERSION
    title: str
    as_of: datetime | None = Field(description="Time of the latest event in the library.")
    stats: SnapshotStats
    sources: list[SourceView]
    lineages: list[LineageView]
    events: list[EventView]
    timepoints: list[TimepointView]
    story: list[StoryStep] = Field(default_factory=list)


def snapshot_json_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) of :class:`LibrarySnapshot`."""

    schema = LibrarySnapshot.model_json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
    }
