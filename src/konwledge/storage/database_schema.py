"""Repository for immutable database schema snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from konwledge.database_schema.models import (
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
)
from konwledge.storage.repository import ImmutableEntityError
from konwledge.storage.tables import DatabaseSchemaSnapshotRow


class DatabaseSchemaSnapshotRepository:
    """Append-only snapshot history keyed by ``(source_id, version)``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: DatabaseSchemaSnapshotRecord) -> None:
        self._session.add(
            DatabaseSchemaSnapshotRow(
                source_id=record.source_id,
                version=record.version,
                content_hash=record.content_hash,
                payload_json=record.snapshot.model_dump(mode="json"),
                captured_at=record.captured_at.isoformat(),
                document_id=record.document_id,
            )
        )
        self._session.flush()

    def get(self, source_id: UUID, version: int) -> DatabaseSchemaSnapshotRecord | None:
        row = self._session.get(DatabaseSchemaSnapshotRow, (source_id, version))
        return None if row is None else self._to_entity(row)

    def get_by_hash(
        self, source_id: UUID, content_hash: str
    ) -> DatabaseSchemaSnapshotRecord | None:
        row = self._session.scalar(
            select(DatabaseSchemaSnapshotRow)
            .where(
                DatabaseSchemaSnapshotRow.source_id == source_id,
                DatabaseSchemaSnapshotRow.content_hash == content_hash,
            )
            .order_by(DatabaseSchemaSnapshotRow.version.desc())
            .limit(1)
        )
        return None if row is None else self._to_entity(row)

    def get_by_document(self, document_id: UUID) -> DatabaseSchemaSnapshotRecord | None:
        row = self._session.scalar(
            select(DatabaseSchemaSnapshotRow)
            .where(DatabaseSchemaSnapshotRow.document_id == document_id)
            .order_by(DatabaseSchemaSnapshotRow.version.desc())
            .limit(1)
        )
        return None if row is None else self._to_entity(row)

    def latest(self, source_id: UUID) -> DatabaseSchemaSnapshotRecord | None:
        row = self._session.scalar(
            select(DatabaseSchemaSnapshotRow)
            .where(DatabaseSchemaSnapshotRow.source_id == source_id)
            .order_by(DatabaseSchemaSnapshotRow.version.desc())
            .limit(1)
        )
        return None if row is None else self._to_entity(row)

    def list_by_source(self, source_id: UUID) -> Sequence[DatabaseSchemaSnapshotRecord]:
        rows = self._session.scalars(
            select(DatabaseSchemaSnapshotRow)
            .where(DatabaseSchemaSnapshotRow.source_id == source_id)
            .order_by(DatabaseSchemaSnapshotRow.version)
        ).all()
        return [self._to_entity(row) for row in rows]

    def list(self) -> Sequence[DatabaseSchemaSnapshotRecord]:
        rows = self._session.scalars(
            select(DatabaseSchemaSnapshotRow).order_by(
                DatabaseSchemaSnapshotRow.source_id,
                DatabaseSchemaSnapshotRow.version,
            )
        ).all()
        return [self._to_entity(row) for row in rows]

    def next_version(self, source_id: UUID) -> int:
        current = self._session.scalar(
            select(func.max(DatabaseSchemaSnapshotRow.version)).where(
                DatabaseSchemaSnapshotRow.source_id == source_id
            )
        )
        return 1 if current is None else int(current) + 1

    def update(self, record: DatabaseSchemaSnapshotRecord) -> None:
        del record
        raise ImmutableEntityError("database schema snapshot history is append-only")

    def delete(self, source_id: UUID, version: int) -> bool:
        del source_id, version
        raise ImmutableEntityError("database schema snapshot history is append-only")

    @staticmethod
    def _to_entity(row: DatabaseSchemaSnapshotRow) -> DatabaseSchemaSnapshotRecord:
        return DatabaseSchemaSnapshotRecord(
            source_id=row.source_id,
            version=row.version,
            snapshot=DatabaseSchemaSnapshot.model_validate(row.payload_json),
            content_hash=row.content_hash,
            captured_at=datetime.fromisoformat(row.captured_at),
            document_id=row.document_id,
        )
