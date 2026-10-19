"""Coordinate file ingestion with idempotent knowledge-unit persistence."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from konwledge.domain import Document, KnowledgeUnit
from konwledge.ingestion.service import FileIngestionService, IngestionResult
from konwledge.ingestion.splitting import TraceableTextSplitter
from konwledge.storage import SessionFactory, UnitOfWork


@dataclass(frozen=True, slots=True)
class SplittingResult:
    """Persisted units and whether this call created them."""

    units: tuple[KnowledgeUnit, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class TraceableIngestionResult:
    """Document ingestion result paired with persisted traceable units."""

    ingestion: IngestionResult
    units: tuple[KnowledgeUnit, ...]
    units_created: bool


class KnowledgeUnitService:
    """Split and persist units once for each immutable document version."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        splitter: TraceableTextSplitter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._splitter = splitter or TraceableTextSplitter()

    def ensure_units(self, document: Document, content: str) -> SplittingResult:
        with UnitOfWork(self._session_factory) as uow:
            existing = tuple(uow.knowledge_units.list_by_document(document.id))
            if existing:
                return SplittingResult(units=existing, created=False)

            units = self._splitter.split(document, content)
            if not units:
                return SplittingResult(units=(), created=False)

            try:
                for unit in units:
                    uow.knowledge_units.add(unit)
                uow.commit()
            except IntegrityError:
                uow.rollback()
                existing = tuple(uow.knowledge_units.list_by_document(document.id))
                if not existing:
                    raise
                return SplittingResult(units=existing, created=False)

            return SplittingResult(units=units, created=True)


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
        )
