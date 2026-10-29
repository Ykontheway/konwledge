"""Repository interfaces and SQLAlchemy implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from konwledge.domain import (
    Citation,
    Document,
    KnowledgeUnit,
    Revision,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.storage.tables import (
    CitationRow,
    DocumentRow,
    KnowledgeUnitRow,
    RevisionRow,
    SourceRow,
)

EntityT = TypeVar("EntityT")
RowT = TypeVar("RowT")


class Repository(Protocol[EntityT]):
    """Persistence operations available to application services."""

    def add(self, entity: EntityT) -> None: ...

    def get(self, entity_id: UUID) -> EntityT | None: ...

    def list(self) -> Sequence[EntityT]: ...

    def update(self, entity: EntityT) -> None: ...

    def delete(self, entity_id: UUID) -> bool: ...


class EntityNotFoundError(LookupError):
    """Raised when an update targets an entity that is not stored."""


class ImmutableEntityError(TypeError):
    """Raised when immutable history is updated or deleted."""


class SqlAlchemyRepository(ABC, Generic[EntityT, RowT]):
    """Shared synchronous repository behavior with explicit model mapping."""

    def __init__(self, session: Session, row_type: type[RowT]) -> None:
        self._session = session
        self._row_type = row_type

    def add(self, entity: EntityT) -> None:
        self._session.add(self._to_row(entity))
        self._session.flush()

    def get(self, entity_id: UUID) -> EntityT | None:
        row = self._session.get(self._row_type, entity_id)
        return None if row is None else self._to_entity(row)

    def list(self) -> Sequence[EntityT]:
        rows = self._session.scalars(select(self._row_type)).all()
        return [self._to_entity(row) for row in rows]

    def update(self, entity: EntityT) -> None:
        entity_id = self._entity_id(entity)
        row = self._session.get(self._row_type, entity_id)
        if row is None:
            raise EntityNotFoundError(f"{self._row_type.__name__} {entity_id} was not found")
        self._apply(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> bool:
        row = self._session.get(self._row_type, entity_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    @abstractmethod
    def _entity_id(self, entity: EntityT) -> UUID:
        raise NotImplementedError

    @abstractmethod
    def _to_row(self, entity: EntityT) -> RowT:
        raise NotImplementedError

    @abstractmethod
    def _to_entity(self, row: RowT) -> EntityT:
        raise NotImplementedError

    @abstractmethod
    def _apply(self, row: RowT, entity: EntityT) -> None:
        raise NotImplementedError


class SourceRepository(SqlAlchemyRepository[Source, SourceRow]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SourceRow)

    def get_by_kind_uri(self, *, kind: SourceKind, uri: str) -> Source | None:
        row = self._session.scalar(
            select(SourceRow).where(SourceRow.kind == kind.value, SourceRow.uri == uri)
        )
        return None if row is None else self._to_entity(row)

    def _entity_id(self, entity: Source) -> UUID:
        return entity.id

    def _to_row(self, entity: Source) -> SourceRow:
        return SourceRow(
            id=entity.id,
            kind=entity.kind.value,
            uri=entity.uri,
            title=entity.title,
            created_at=entity.created_at.isoformat(),
            metadata_json=entity.metadata,
        )

    def _to_entity(self, row: SourceRow) -> Source:
        return Source.model_validate(
            {
                "id": row.id,
                "kind": row.kind,
                "uri": row.uri,
                "title": row.title,
                "created_at": row.created_at,
                "metadata": row.metadata_json,
            }
        )

    def _apply(self, row: SourceRow, entity: Source) -> None:
        row.kind = entity.kind.value
        row.uri = entity.uri
        row.title = entity.title
        row.created_at = entity.created_at.isoformat()
        row.metadata_json = entity.metadata


class DocumentRepository(SqlAlchemyRepository[Document, DocumentRow]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, DocumentRow)

    def get_by_import_identity(
        self, *, source_id: UUID, locator: str, checksum: str
    ) -> Document | None:
        row = self._session.scalar(
            select(DocumentRow).where(
                DocumentRow.source_id == source_id,
                DocumentRow.locator == locator,
                DocumentRow.checksum == checksum,
            )
        )
        return None if row is None else self._to_entity(row)

    def list_by_source(self, source_id: UUID) -> Sequence[Document]:
        """Return all document versions of one source, oldest first."""

        rows = self._session.scalars(
            select(DocumentRow).where(DocumentRow.source_id == source_id)
        ).all()
        documents = [self._to_entity(row) for row in rows]
        return sorted(documents, key=lambda document: (document.ingested_at, str(document.id)))

    def _entity_id(self, entity: Document) -> UUID:
        return entity.id

    def _to_row(self, entity: Document) -> DocumentRow:
        return DocumentRow(
            id=entity.id,
            source_id=entity.source_id,
            locator=entity.locator,
            content_type=entity.content_type.value,
            checksum=entity.checksum,
            ingested_at=entity.ingested_at.isoformat(),
            metadata_json=entity.metadata,
        )

    def _to_entity(self, row: DocumentRow) -> Document:
        return Document.model_validate(
            {
                "id": row.id,
                "source_id": row.source_id,
                "locator": row.locator,
                "content_type": row.content_type,
                "checksum": row.checksum,
                "ingested_at": row.ingested_at,
                "metadata": row.metadata_json,
            }
        )

    def _apply(self, row: DocumentRow, entity: Document) -> None:
        row.source_id = entity.source_id
        row.locator = entity.locator
        row.content_type = entity.content_type.value
        row.checksum = entity.checksum
        row.ingested_at = entity.ingested_at.isoformat()
        row.metadata_json = entity.metadata


class KnowledgeUnitRepository(SqlAlchemyRepository[KnowledgeUnit, KnowledgeUnitRow]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, KnowledgeUnitRow)

    def list_by_document(self, document_id: UUID) -> Sequence[KnowledgeUnit]:
        rows = self._session.scalars(
            select(KnowledgeUnitRow)
            .where(KnowledgeUnitRow.document_id == document_id)
            .order_by(KnowledgeUnitRow.ordinal)
        ).all()
        return [self._to_entity(row) for row in rows]

    def _entity_id(self, entity: KnowledgeUnit) -> UUID:
        return entity.id

    def list_by_lineage(self, lineage_id: UUID) -> Sequence[KnowledgeUnit]:
        """Return every stored version of one lineage (legacy rows count as their own)."""

        rows = self._session.scalars(
            select(KnowledgeUnitRow).where(
                or_(
                    KnowledgeUnitRow.lineage_id == lineage_id,
                    and_(
                        KnowledgeUnitRow.lineage_id.is_(None),
                        KnowledgeUnitRow.id == lineage_id,
                    ),
                )
            )
        ).all()
        return [self._to_entity(row) for row in rows]

    def _to_row(self, entity: KnowledgeUnit) -> KnowledgeUnitRow:
        return KnowledgeUnitRow(
            id=entity.id,
            lineage_id=entity.lineage_id,
            document_id=entity.document_id,
            ordinal=entity.ordinal,
            text=entity.text,
            start_char=entity.start_char,
            end_char=entity.end_char,
            heading=entity.heading,
            section_path=list(entity.section_path),
            status=entity.status.value,
            tags=sorted(entity.tags),
            metadata_json=entity.metadata,
        )

    def _to_entity(self, row: KnowledgeUnitRow) -> KnowledgeUnit:
        return KnowledgeUnit.model_validate(
            {
                "id": row.id,
                "lineage_id": row.lineage_id,
                "document_id": row.document_id,
                "ordinal": row.ordinal,
                "text": row.text,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "heading": row.heading,
                "section_path": tuple(row.section_path),
                "status": row.status,
                "tags": set(row.tags),
                "metadata": row.metadata_json,
            }
        )

    def _apply(self, row: KnowledgeUnitRow, entity: KnowledgeUnit) -> None:
        row.lineage_id = entity.lineage_id
        row.document_id = entity.document_id
        row.ordinal = entity.ordinal
        row.text = entity.text
        row.start_char = entity.start_char
        row.end_char = entity.end_char
        row.heading = entity.heading
        row.section_path = list(entity.section_path)
        row.status = entity.status.value
        row.tags = sorted(entity.tags)
        row.metadata_json = entity.metadata


class RevisionRepository(SqlAlchemyRepository[Revision, RevisionRow]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, RevisionRow)

    def update(self, entity: Revision) -> None:
        raise ImmutableEntityError("revision history is append-only")

    def delete(self, entity_id: UUID) -> bool:
        raise ImmutableEntityError("revision history is append-only")

    def list_for(self, entity_type: RevisionEntityType, entity_id: UUID) -> Sequence[Revision]:
        """Return the revision chain of one entity in ascending revision order."""

        rows = self._session.scalars(
            select(RevisionRow)
            .where(
                RevisionRow.entity_type == entity_type.value,
                RevisionRow.entity_id == entity_id,
            )
            .order_by(RevisionRow.revision)
        ).all()
        return [self._to_entity(row) for row in rows]

    def latest_for(self, entity_type: RevisionEntityType, entity_id: UUID) -> Revision | None:
        """Return the newest revision of one entity, if any."""

        row = self._session.scalar(
            select(RevisionRow)
            .where(
                RevisionRow.entity_type == entity_type.value,
                RevisionRow.entity_id == entity_id,
            )
            .order_by(RevisionRow.revision.desc())
            .limit(1)
        )
        return None if row is None else self._to_entity(row)

    def next_number(self, entity_type: RevisionEntityType, entity_id: UUID) -> int:
        """Return the revision number the next change of one entity should use."""

        current = self._session.scalar(
            select(func.max(RevisionRow.revision)).where(
                RevisionRow.entity_type == entity_type.value,
                RevisionRow.entity_id == entity_id,
            )
        )
        return 1 if current is None else int(current) + 1

    def _entity_id(self, entity: Revision) -> UUID:
        return entity.id

    def _to_row(self, entity: Revision) -> RevisionRow:
        return RevisionRow(
            id=entity.id,
            entity_type=entity.entity_type.value,
            entity_id=entity.entity_id,
            revision=entity.revision,
            change_type=entity.change_type.value,
            created_at=entity.created_at.isoformat(),
            author=entity.author,
            payload=entity.payload,
        )

    def _to_entity(self, row: RevisionRow) -> Revision:
        return Revision.model_validate(
            {
                "id": row.id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "revision": row.revision,
                "change_type": row.change_type,
                "created_at": row.created_at,
                "author": row.author,
                "payload": row.payload,
            }
        )

    def _apply(self, row: RevisionRow, entity: Revision) -> None:
        row.entity_type = entity.entity_type.value
        row.entity_id = entity.entity_id
        row.revision = entity.revision
        row.change_type = entity.change_type.value
        row.created_at = entity.created_at.isoformat()
        row.author = entity.author
        row.payload = entity.payload


class CitationRepository(SqlAlchemyRepository[Citation, CitationRow]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, CitationRow)

    def _entity_id(self, entity: Citation) -> UUID:
        return entity.id

    def _to_row(self, entity: Citation) -> CitationRow:
        return CitationRow(
            id=entity.id,
            source_id=entity.source_id,
            document_id=entity.document_id,
            knowledge_unit_id=entity.knowledge_unit_id,
            quote=entity.quote,
            locator=entity.locator,
            start_char=entity.start_char,
            end_char=entity.end_char,
            confidence=entity.confidence,
            metadata_json=entity.metadata,
        )

    def _to_entity(self, row: CitationRow) -> Citation:
        return Citation.model_validate(
            {
                "id": row.id,
                "source_id": row.source_id,
                "document_id": row.document_id,
                "knowledge_unit_id": row.knowledge_unit_id,
                "quote": row.quote,
                "locator": row.locator,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "confidence": row.confidence,
                "metadata": row.metadata_json,
            }
        )

    def _apply(self, row: CitationRow, entity: Citation) -> None:
        row.source_id = entity.source_id
        row.document_id = entity.document_id
        row.knowledge_unit_id = entity.knowledge_unit_id
        row.quote = entity.quote
        row.locator = entity.locator
        row.start_char = entity.start_char
        row.end_char = entity.end_char
        row.confidence = entity.confidence
        row.metadata_json = entity.metadata
