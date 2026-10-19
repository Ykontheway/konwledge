"""Application service for idempotent local text ingestion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from konwledge.domain import Document, Source, SourceKind
from konwledge.ingestion.loaders import LoadedText, TextFileLoader
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


class SourceNotFoundError(LookupError):
    """Raised when ingestion targets a source that is not stored."""


class UnsupportedSourceKindError(ValueError):
    """Raised when file ingestion receives a non-file source."""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """A persisted document paired with its normalized content."""

    document: Document
    content: str
    created: bool


class FileIngestionService:
    """Load one file source and persist its document metadata idempotently."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        loader: TextFileLoader | None = None,
        id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._loader = loader or TextFileLoader()
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def ingest(self, source_id: UUID) -> IngestionResult:
        with UnitOfWork(self._session_factory) as uow:
            source = self._get_file_source(uow, source_id)
            loaded = self._loader.load(source.uri)
            existing = uow.documents.get_by_import_identity(
                source_id=source.id,
                locator=source.uri,
                checksum=loaded.checksum,
            )
            if existing is not None:
                return IngestionResult(document=existing, content=loaded.content, created=False)

            document = self._build_document(source.id, source.uri, loaded)
            try:
                uow.documents.add(document)
            except IntegrityError:
                uow.rollback()
                existing = uow.documents.get_by_import_identity(
                    source_id=source.id,
                    locator=source.uri,
                    checksum=loaded.checksum,
                )
                if existing is None:
                    raise
                return IngestionResult(document=existing, content=loaded.content, created=False)

            uow.commit()
            return IngestionResult(document=document, content=loaded.content, created=True)

    def _get_file_source(self, uow: UnitOfWork, source_id: UUID) -> Source:
        source = uow.sources.get(source_id)
        if source is None:
            raise SourceNotFoundError(f"source {source_id} was not found")
        if source.kind is not SourceKind.FILE:
            raise UnsupportedSourceKindError(
                f"source {source_id} has kind {source.kind.value}; expected file"
            )
        return source

    def _build_document(self, source_id: UUID, locator: str, loaded: LoadedText) -> Document:
        return Document(
            id=self._id_factory(),
            source_id=source_id,
            locator=locator,
            content_type=loaded.content_type,
            checksum=loaded.checksum,
            ingested_at=self._clock(),
        )
