"""Persistence for complete immutable evaluation dataset versions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from konwledge.domain import EvaluationSet
from konwledge.storage.repository import ImmutableEntityError
from konwledge.storage.tables import EvaluationSetRow


class EvaluationSetRepository:
    """Store each dataset version as one self-contained immutable snapshot."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, dataset: EvaluationSet) -> None:
        self._session.add(
            EvaluationSetRow(
                id=dataset.id,
                version=dataset.version,
                name=dataset.name,
                created_at=dataset.created_at.isoformat(),
                content_hash=dataset.content_hash,
                payload=dataset.model_dump(mode="json"),
            )
        )
        self._session.flush()

    def get(self, set_id: UUID, version: int | None = None) -> EvaluationSet | None:
        statement = select(EvaluationSetRow).where(EvaluationSetRow.id == set_id)
        if version is None:
            statement = statement.order_by(EvaluationSetRow.version.desc()).limit(1)
        else:
            statement = statement.where(EvaluationSetRow.version == version)
        row = self._session.scalar(statement)
        return None if row is None else self._to_entity(row)

    def list_versions(self, set_id: UUID) -> Sequence[EvaluationSet]:
        rows = self._session.scalars(
            select(EvaluationSetRow)
            .where(EvaluationSetRow.id == set_id)
            .order_by(EvaluationSetRow.version)
        ).all()
        return [self._to_entity(row) for row in rows]

    def list(self) -> Sequence[EvaluationSet]:
        rows = self._session.scalars(
            select(EvaluationSetRow).order_by(EvaluationSetRow.id, EvaluationSetRow.version)
        ).all()
        latest: dict[UUID, EvaluationSet] = {}
        for row in rows:
            latest[row.id] = self._to_entity(row)
        return [latest[set_id] for set_id in sorted(latest, key=str)]

    def update(self, dataset: EvaluationSet) -> None:
        raise ImmutableEntityError("evaluation dataset versions are immutable")

    def delete(self, set_id: UUID, version: int | None = None) -> bool:
        raise ImmutableEntityError("evaluation dataset versions are immutable")

    @staticmethod
    def _to_entity(row: EvaluationSetRow) -> EvaluationSet:
        try:
            payload_json = json.dumps(row.payload, ensure_ascii=False)
            dataset = EvaluationSet.model_validate_json(payload_json)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"evaluation dataset {row.id} version {row.version} has an invalid payload"
            ) from error
        expected_identity = (
            row.id,
            row.version,
            row.name,
            row.created_at,
            row.content_hash,
        )
        actual_identity = (
            dataset.id,
            dataset.version,
            dataset.name,
            dataset.created_at.isoformat(),
            dataset.content_hash,
        )
        if expected_identity != actual_identity:
            raise ValueError(
                f"evaluation dataset {row.id} version {row.version} failed integrity verification"
            )
        return dataset
