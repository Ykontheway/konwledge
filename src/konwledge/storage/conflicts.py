"""Conflict repository backed by SQLAlchemy and SQLite."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from konwledge.domain import Conflict
from konwledge.storage.repository import (
    EntityNotFoundError,
    ImmutableEntityError,
    SqlAlchemyRepository,
)
from konwledge.storage.tables import ConflictRow


class ConflictRepository(SqlAlchemyRepository[Conflict, ConflictRow]):
    """Persist immutable conflict evidence with mutable review state."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, ConflictRow)

    def get_by_key(self, key: str) -> Conflict | None:
        row = self._session.scalar(select(ConflictRow).where(ConflictRow.key == key))
        return None if row is None else self._to_entity(row)

    def list(self) -> Sequence[Conflict]:
        rows = self._session.scalars(select(ConflictRow)).all()
        conflicts = [self._to_entity(row) for row in rows]
        return sorted(conflicts, key=lambda conflict: (conflict.detected_at, str(conflict.id)))

    def update(self, entity: Conflict) -> None:
        row = self._session.get(ConflictRow, entity.id)
        if row is None:
            key_row = self._session.scalar(select(ConflictRow).where(ConflictRow.key == entity.key))
            if key_row is not None:
                raise ImmutableEntityError("conflict identity is immutable")
            signature_row = self._find_by_immutable_signature(entity)
            if signature_row is not None:
                raise ImmutableEntityError("conflict identity is immutable")
            raise EntityNotFoundError(f"ConflictRow {entity.id} was not found")
        self._apply(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> bool:
        raise ImmutableEntityError("conflict evidence is immutable")

    def _entity_id(self, entity: Conflict) -> UUID:
        return entity.id

    def _to_row(self, entity: Conflict) -> ConflictRow:
        return ConflictRow(
            id=entity.id,
            key=entity.key,
            status=entity.status.value,
            detected_at=entity.detected_at.isoformat(),
            left_source_id=entity.left.source_id,
            left_document_id=entity.left.document_id,
            left_unit_id=entity.left.unit_id,
            right_source_id=entity.right.source_id,
            right_document_id=entity.right.document_id,
            right_unit_id=entity.right.unit_id,
            payload=entity.model_dump(mode="json"),
        )

    def _to_entity(self, row: ConflictRow) -> Conflict:
        return Conflict.model_validate(row.payload)

    def _apply(self, row: ConflictRow, entity: Conflict) -> None:
        current = self._to_entity(row)
        if current.id != entity.id:
            raise ImmutableEntityError("conflict id is immutable")
        if current.key != entity.key:
            raise ImmutableEntityError("conflict key is immutable")
        if current.rule != entity.rule:
            raise ImmutableEntityError("conflict rule is immutable")
        if current.reason != entity.reason:
            raise ImmutableEntityError("conflict reason is immutable")
        if current.left != entity.left or current.right != entity.right:
            raise ImmutableEntityError("conflict evidence is immutable")
        if current.detected_at != entity.detected_at:
            raise ImmutableEntityError("conflict detected_at is immutable")
        row.status = entity.status.value
        row.payload = entity.model_dump(mode="json")

    def _find_by_immutable_signature(self, entity: Conflict) -> ConflictRow | None:
        rows = self._session.scalars(select(ConflictRow)).all()
        for row in rows:
            current = self._to_entity(row)
            if (
                current.rule == entity.rule
                and current.reason == entity.reason
                and current.left == entity.left
                and current.right == entity.right
                and current.detected_at == entity.detected_at
            ):
                return row
        return None
