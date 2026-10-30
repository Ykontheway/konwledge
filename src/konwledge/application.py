"""Application services for importing and inspecting a local knowledge library."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from konwledge.domain import (
    Document,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionResult,
    TraceableIngestionService,
)
from konwledge.lifecycle import (
    KnowledgeSnapshot,
    KnowledgeUnitNotFoundError,
    LifecycleService,
    UnitState,
)
from konwledge.storage import SessionFactory, UnitOfWork

__all__ = [
    "SUPPORTED_SUFFIXES",
    "DocumentSummary",
    "FileDiscovery",
    "ImportPathError",
    "ImportReport",
    "ImportedFile",
    "KnowledgeLibrary",
    "KnowledgeUnitDetail",
    "KnowledgeUnitNotFoundError",
    "UnitHistory",
    "discover_import_files",
]

SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


class ImportPathError(ValueError):
    """Raised when an import target cannot produce supported files."""


@dataclass(frozen=True, slots=True)
class FileDiscovery:
    """Stable supported-file discovery plus ignored files."""

    files: tuple[Path, ...]
    skipped: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class ImportedFile:
    """Result of importing one discovered file."""

    path: Path
    result: TraceableIngestionResult


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Complete result for a single-file or directory import."""

    imported: tuple[ImportedFile, ...]
    skipped: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    """A document, its source, its traceable units, and their current statuses.

    ``is_head`` marks the version currently published for the source;
    ``statuses`` is empty for a version that was never published.
    """

    document: Document
    source: Source
    units: tuple[KnowledgeUnit, ...]
    is_head: bool = False
    statuses: Mapping[UUID, KnowledgeUnitStatus] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeUnitDetail:
    """A knowledge unit with its document and source context."""

    unit: KnowledgeUnit
    document: Document
    source: Source


@dataclass(frozen=True, slots=True)
class UnitHistory:
    """All stored versions of one knowledge lineage and its revision chain."""

    unit: KnowledgeUnit
    lineage_id: UUID
    versions: tuple[KnowledgeUnit, ...]
    revisions: tuple[Revision, ...]
    statuses: Mapping[UUID, KnowledgeUnitStatus] = field(default_factory=dict)


def discover_import_files(target: Path) -> FileDiscovery:
    """Discover supported files recursively in deterministic relative-path order."""

    path = target.expanduser()
    if not path.exists():
        raise ImportPathError(f"import path does not exist: {path}")
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise ImportPathError(
                f"unsupported file extension {path.suffix or '<none>'}; "
                f"expected one of: {supported}"
            )
        return FileDiscovery(files=(path.resolve(),), skipped=())
    if not path.is_dir():
        raise ImportPathError(f"import path is neither a regular file nor a directory: {path}")

    all_files = sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    )
    files = tuple(candidate.resolve() for candidate in all_files if _is_supported(candidate))
    skipped = tuple(candidate.resolve() for candidate in all_files if not _is_supported(candidate))
    if not files:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ImportPathError(f"no supported files found in {path} (expected: {supported})")
    return FileDiscovery(files=files, skipped=skipped)


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def _statuses_by_unit(snapshot: KnowledgeSnapshot) -> dict[UUID, KnowledgeUnitStatus]:
    return {
        state.unit.id: state.status
        for source in snapshot.sources
        for state in (*source.units, *source.retired)
    }


class KnowledgeLibrary:
    """Import files and query persisted knowledge through UnitOfWork boundaries."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
        ingestion_service: TraceableIngestionService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ingestion_service = ingestion_service or TraceableIngestionService(
            FileIngestionService(session_factory),
            KnowledgeUnitService(session_factory),
        )
        self._lifecycle = LifecycleService(session_factory, id_factory=id_factory, clock=clock)

    @property
    def lifecycle(self) -> LifecycleService:
        """Status queries, as-of snapshots, and explicit stale/refresh marks."""

        return self._lifecycle

    def import_path(self, target: Path) -> ImportReport:
        """Import every supported target file in stable order."""

        discovery = discover_import_files(target)
        imported = tuple(self._import_file(path) for path in discovery.files)
        return ImportReport(imported=imported, skipped=discovery.skipped)

    def list_documents(self) -> tuple[DocumentSummary, ...]:
        """Return all document versions with source and unit details in stable order."""

        current = self._lifecycle.snapshot(include_retired=True)
        heads = {snapshot.head.id for snapshot in current.sources if snapshot.head is not None}
        statuses = _statuses_by_unit(current)
        with UnitOfWork(self._session_factory) as uow:
            documents = sorted(
                uow.documents.list(),
                key=lambda document: (
                    document.locator.casefold(),
                    document.locator,
                    document.ingested_at,
                    str(document.id),
                ),
            )
            summaries: list[DocumentSummary] = []
            for document in documents:
                source = uow.sources.get(document.source_id)
                if source is None:
                    raise RuntimeError(f"document {document.id} references a missing source")
                units = tuple(uow.knowledge_units.list_by_document(document.id))
                summaries.append(
                    DocumentSummary(
                        document=document,
                        source=source,
                        units=units,
                        is_head=document.id in heads,
                        statuses={
                            unit.id: statuses[unit.id] for unit in units if unit.id in statuses
                        },
                    )
                )
            return tuple(summaries)

    def snapshot(
        self, at: datetime | None = None, *, include_retired: bool = False
    ) -> KnowledgeSnapshot:
        """Return the effective knowledge at ``at`` (now when omitted)."""

        return self._lifecycle.snapshot(at, include_retired=include_retired)

    def unit_state(self, unit_id: UUID, at: datetime | None = None) -> UnitState | None:
        """Return a unit's status at ``at``; ``None`` if not yet published then."""

        return self._lifecycle.unit_state(unit_id, at)

    def show_unit(self, unit_id: UUID) -> KnowledgeUnitDetail:
        """Return one unit with its document and source, or raise a clear lookup error."""

        with UnitOfWork(self._session_factory) as uow:
            unit = uow.knowledge_units.get(unit_id)
            if unit is None:
                raise KnowledgeUnitNotFoundError(f"knowledge unit {unit_id} was not found")
            document = uow.documents.get(unit.document_id)
            if document is None:
                raise RuntimeError(f"knowledge unit {unit.id} references a missing document")
            source = uow.sources.get(document.source_id)
            if source is None:
                raise RuntimeError(f"document {document.id} references a missing source")
            return KnowledgeUnitDetail(unit=unit, document=document, source=source)

    def unit_history(self, unit_id: UUID) -> UnitHistory:
        """Return every stored version and the revision chain of a unit's lineage."""

        with UnitOfWork(self._session_factory) as uow:
            unit = uow.knowledge_units.get(unit_id)
            if unit is None:
                raise KnowledgeUnitNotFoundError(f"knowledge unit {unit_id} was not found")
            versions = tuple(uow.knowledge_units.list_by_lineage(unit.lineage_id))
            ingested_at: dict[UUID, datetime] = {}
            source_ids: set[UUID] = set()
            for version in versions:
                document = uow.documents.get(version.document_id)
                if document is None:
                    raise RuntimeError(f"knowledge unit {version.id} references a missing document")
                ingested_at[version.document_id] = document.ingested_at
                source_ids.add(document.source_id)
            versions = tuple(
                sorted(
                    versions,
                    key=lambda version: (ingested_at[version.document_id], str(version.id)),
                )
            )
            revisions = tuple(
                uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, unit.lineage_id)
            )
        statuses: dict[UUID, KnowledgeUnitStatus] = {}
        for source_id in source_ids:
            current = self._lifecycle.snapshot(include_retired=True, source_id=source_id)
            statuses.update(_statuses_by_unit(current))
        return UnitHistory(
            unit=unit,
            lineage_id=unit.lineage_id,
            versions=versions,
            revisions=revisions,
            statuses={
                version.id: statuses[version.id] for version in versions if version.id in statuses
            },
        )

    def _import_file(self, path: Path) -> ImportedFile:
        source = self._ensure_file_source(path)
        return ImportedFile(path=path, result=self._ingestion_service.ingest(source.id))

    def _ensure_file_source(self, path: Path) -> Source:
        uri = str(path.resolve())
        with UnitOfWork(self._session_factory) as uow:
            existing = uow.sources.get_by_kind_uri(kind=SourceKind.FILE, uri=uri)
            if existing is not None:
                return existing

            source = Source(
                id=self._id_factory(),
                kind=SourceKind.FILE,
                uri=uri,
                title=path.name,
                created_at=self._clock(),
            )
            try:
                uow.sources.add(source)
                uow.commit()
            except IntegrityError:
                uow.rollback()
                existing = uow.sources.get_by_kind_uri(kind=SourceKind.FILE, uri=uri)
                if existing is None:
                    raise
                return existing
            return source
