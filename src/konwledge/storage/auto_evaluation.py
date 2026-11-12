"""SQLite persistence helpers for automatic evaluation configuration and jobs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from konwledge.auto_evaluation.models import (
    AutoEvaluationConfig,
    AutoEvaluationJob,
    AutoEvaluationStatus,
)
from konwledge.storage.evaluation import EvaluationSetRepository
from konwledge.storage.tables import AutoEvaluationConfigRow, AutoEvaluationJobRow, RevisionRow


def canonical_hash(value: object) -> str:
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def config_fingerprint(config: AutoEvaluationConfig, dataset_payload: object) -> str:
    payload = config.model_dump(mode="json", exclude={"updated_at"})
    return canonical_hash({"config": payload, "dataset": dataset_payload})


def knowledge_fingerprint(session: Session) -> str:
    """Hash complete relevant revision records without persisting their plaintext payloads."""

    rows = session.scalars(
        select(RevisionRow)
        .where(RevisionRow.entity_type.in_(("source", "knowledge_unit")))
        .order_by(
            RevisionRow.created_at,
            RevisionRow.entity_type,
            RevisionRow.entity_id,
            RevisionRow.revision,
            RevisionRow.id,
        )
    ).all()
    changes: list[dict[str, object]] = []
    for row in rows:
        if not is_knowledge_revision(row):
            continue
        changes.append(
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": str(row.entity_id),
                "revision": row.revision,
                "change_type": row.change_type,
                "created_at": row.created_at,
                "author": row.author,
                "payload_hash": canonical_hash(row.payload),
            }
        )
    return canonical_hash(changes)


def is_knowledge_revision(row: RevisionRow) -> bool:
    change = row.payload.get("change")
    return (row.entity_type == "source" and change in {"published", "freshness_policy"}) or (
        row.entity_type == "knowledge_unit"
        and change in {"added", "modified", "deleted", "stale", "refreshed"}
    )


class AutoEvaluationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def config(self) -> AutoEvaluationConfig | None:
        pinned = self.config_with_fingerprint()
        return None if pinned is None else pinned[0]

    def config_with_fingerprint(self) -> tuple[AutoEvaluationConfig, str] | None:
        row = self._session.get(AutoEvaluationConfigRow, 1)
        if row is None:
            return None
        config = self._config_entity(row)
        dataset = EvaluationSetRepository(self._session).get(
            config.dataset_id, config.dataset_version
        )
        if dataset is None or dataset.content_hash != config.dataset_hash:
            raise ValueError("automatic evaluation dataset pin failed integrity verification")
        actual = config_fingerprint(config, dataset.model_dump(mode="json"))
        if actual != row.config_fingerprint:
            raise ValueError("automatic evaluation configuration failed integrity verification")
        return config, row.config_fingerprint

    def put_config(self, config: AutoEvaluationConfig) -> None:
        dataset = EvaluationSetRepository(self._session).get(
            config.dataset_id, config.dataset_version
        )
        if dataset is None or dataset.content_hash != config.dataset_hash:
            raise ValueError("automatic evaluation dataset pin failed integrity verification")
        fingerprint = config_fingerprint(config, dataset.model_dump(mode="json"))
        row = self._session.get(AutoEvaluationConfigRow, 1)
        payload = config.model_dump(mode="json")
        if row is None:
            self._session.add(
                AutoEvaluationConfigRow(
                    id=1,
                    generation=config.generation,
                    enabled=int(config.enabled),
                    dataset_id=config.dataset_id,
                    dataset_version=config.dataset_version,
                    dataset_hash=config.dataset_hash,
                    config_fingerprint=fingerprint,
                    payload=payload,
                    updated_at=config.updated_at.isoformat(),
                )
            )
        else:
            row.generation = config.generation
            row.enabled = int(config.enabled)
            row.dataset_id = config.dataset_id
            row.dataset_version = config.dataset_version
            row.dataset_hash = config.dataset_hash
            row.config_fingerprint = fingerprint
            row.payload = payload
            row.updated_at = config.updated_at.isoformat()
        self._session.flush()

    def add_job_row(self, row: AutoEvaluationJobRow) -> None:
        self._session.add(row)
        self._session.flush()

    def update_job_status(
        self,
        job_id: UUID,
        token: UUID,
        status: str,
        *,
        finished_at: str | None = None,
        report: dict[str, Any] | None = None,
        error_code: str | None = None,
        attempt_increment: int = 0,
        recovery_error: dict[str, Any] | None = None,
    ) -> bool:
        """Atomic lease-token CAS update for terminal status or crash recovery."""
        values: dict[str, Any] = {
            "status": status,
            "finished_at": finished_at,
            "lease_token": None,
            "lease_expires_at": None,
            "report": report,
            "error_code": error_code,
        }
        if attempt_increment:
            values["attempt_count"] = AutoEvaluationJobRow.attempt_count + attempt_increment

        statement = (
            update(AutoEvaluationJobRow)
            .where(
                AutoEvaluationJobRow.id == job_id,
                AutoEvaluationJobRow.lease_token == token,
            )
            .values(**values)
        )

        if recovery_error:
            # JSON_ARRAY_APPEND equivalent in SQLAlchemy/SQLite is tricky.
            # We'll just fetch and update if it's not a common case.
            # For now, let's just do it in the session if CAS succeeds.
            pass

        result = self._session.execute(statement)
        if getattr(result, "rowcount", 0) == 1 and recovery_error:
            row = self._session.get(AutoEvaluationJobRow, job_id)
            if row:
                errors = list(row.recovery_errors)
                errors.append(recovery_error)
                row.recovery_errors = errors
        return bool(getattr(result, "rowcount", 0) == 1)

    def get_job(self, job_id: UUID) -> AutoEvaluationJob | None:
        row = self._session.get(AutoEvaluationJobRow, job_id)
        return None if row is None else self._job_entity(row)

    def history(self, *, limit: int = 100) -> Sequence[AutoEvaluationJob]:
        rows = self._session.scalars(
            select(AutoEvaluationJobRow)
            .order_by(AutoEvaluationJobRow.created_at.desc(), AutoEvaluationJobRow.id.desc())
            .limit(limit)
        ).all()
        return [self._job_entity(row) for row in rows]

    def get_dataset_hash(self, dataset_id: UUID, version: int) -> str | None:
        dataset = EvaluationSetRepository(self._session).get(dataset_id, version)
        return None if dataset is None else dataset.content_hash

    @staticmethod
    def _config_entity(row: AutoEvaluationConfigRow) -> AutoEvaluationConfig:
        config = AutoEvaluationConfig.model_validate_json(
            json.dumps(row.payload, ensure_ascii=False, allow_nan=False)
        )
        identity = (
            config.generation,
            int(config.enabled),
            config.dataset_id,
            config.dataset_version,
            config.dataset_hash,
            config.updated_at.isoformat(),
        )
        stored = (
            row.generation,
            row.enabled,
            row.dataset_id,
            row.dataset_version,
            row.dataset_hash,
            row.updated_at,
        )
        if identity != stored:
            raise ValueError("automatic evaluation configuration failed integrity verification")
        return config

    @staticmethod
    def _job_entity(row: AutoEvaluationJobRow) -> AutoEvaluationJob:
        return AutoEvaluationJob(
            id=row.id,
            parent_job_id=row.parent_job_id,
            config_generation=row.config_generation,
            config_fingerprint=row.config_fingerprint,
            dataset_id=row.dataset_id,
            dataset_version=row.dataset_version,
            dataset_hash=row.dataset_hash,
            knowledge_fingerprint=row.knowledge_fingerprint,
            trigger_revision_ids=tuple(UUID(value) for value in row.trigger_revision_ids),
            status=AutoEvaluationStatus(row.status),
            created_at=datetime.fromisoformat(row.created_at),
            evaluated_at=datetime.fromisoformat(row.evaluated_at),
            started_at=None if row.started_at is None else datetime.fromisoformat(row.started_at),
            finished_at=(
                None if row.finished_at is None else datetime.fromisoformat(row.finished_at)
            ),
            lease_expires_at=(
                None
                if row.lease_expires_at is None
                else datetime.fromisoformat(row.lease_expires_at)
            ),
            report=row.report,
            error_code=row.error_code,
            attempt_count=row.attempt_count,
            recovery_errors=tuple(row.recovery_errors),
        )


def make_job_row(
    *,
    job_id: UUID,
    config: AutoEvaluationConfig,
    fingerprint: str,
    knowledge: str,
    revisions: Sequence[UUID],
    now: datetime,
    parent_job_id: UUID | None = None,
    trigger_key: str | None = None,
) -> AutoEvaluationJobRow:
    return AutoEvaluationJobRow(
        id=job_id,
        parent_job_id=parent_job_id,
        trigger_key=trigger_key,
        config_generation=config.generation,
        config_fingerprint=fingerprint,
        dataset_id=config.dataset_id,
        dataset_version=config.dataset_version,
        dataset_hash=config.dataset_hash,
        knowledge_fingerprint=knowledge,
        trigger_revision_ids=[str(value) for value in revisions],
        status=AutoEvaluationStatus.QUEUED.value,
        created_at=now.isoformat(),
        evaluated_at=now.isoformat(),
    )
