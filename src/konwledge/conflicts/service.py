"""Storage-aware conflict scanning and human review."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from konwledge.conflicts.rules import (
    PORT_PREDICATE,
    ConflictCandidate,
    document_service_context,
    extract_port_assertion,
    port_mismatch_candidates,
)
from konwledge.domain import (
    Conflict,
    ConflictEvidence,
    ConflictStatus,
    Document,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
)
from konwledge.lifecycle.service import UnitState, _load_view
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


class ConflictNotFoundError(LookupError):
    """Raised when a requested conflict does not exist."""


class ConflictTransitionError(ValueError):
    """Raised when a conflict review would not perform a valid transition."""


@dataclass(frozen=True, slots=True)
class ConflictScan:
    """Deterministic result of evaluating the knowledge visible at one time."""

    evaluated_at: datetime
    dry_run: bool
    candidates: tuple[ConflictCandidate, ...]
    created: tuple[Conflict, ...]
    existing: tuple[Conflict, ...]


class ConflictService:
    """Detect scalar service-port conflicts and record human decisions."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def scan(self, *, at: datetime | None = None, dry_run: bool = True) -> ConflictScan:
        """Evaluate all source heads, optionally persisting newly detected conflicts."""

        if at is not None and not dry_run:
            raise ValueError("historical conflict scans must be dry runs")
        evaluated_at = _aware(at if at is not None else self._clock(), "evaluated_at")

        with UnitOfWork(self._session_factory) as uow:
            evidence = self._load_evidence(uow, evaluated_at)
            candidates = port_mismatch_candidates(evidence)
            stored_conflicts = uow.conflicts.list()
            visible_existing: dict[str, Conflict] = {}
            for conflict in stored_conflicts:
                if at is not None:
                    if conflict.detected_at > evaluated_at:
                        continue
                    revisions = uow.revisions.list_for(RevisionEntityType.CONFLICT, conflict.id)
                    visible = [
                        revision for revision in revisions if revision.created_at <= evaluated_at
                    ]
                    if not visible:
                        raise RuntimeError(
                            f"conflict {conflict.id} has no visible detection revision"
                        )
                    latest = max(visible, key=lambda revision: revision.revision)
                    reviewed = latest.payload["change"] == "reviewed"
                    # Evidence is immutable; every mutable review field comes from
                    # the visible revision, never from the current database row.
                    conflict = Conflict.model_validate(
                        {
                            **conflict.model_dump(),
                            "status": latest.payload["to_status"]
                            if reviewed
                            else latest.payload["status"],
                            "reviewed_at": latest.created_at if reviewed else None,
                            "review_reason": latest.payload["reason"] if reviewed else None,
                            "reviewer": latest.payload.get("reviewer") if reviewed else None,
                        }
                    )
                visible_existing[conflict.key] = conflict

            existing = tuple(
                visible_existing[candidate.key]
                for candidate in candidates
                if candidate.key in visible_existing
            )
            if dry_run:
                return ConflictScan(
                    evaluated_at=evaluated_at,
                    dry_run=True,
                    candidates=candidates,
                    created=(),
                    existing=existing,
                )

            self._validate_write_view(uow, candidates, evaluated_at)
            created: list[Conflict] = []
            for candidate in candidates:
                if candidate.key in visible_existing:
                    continue
                conflict = Conflict(
                    id=self._id_factory(),
                    key=candidate.key,
                    rule=candidate.rule,
                    reason=candidate.reason,
                    left=candidate.left,
                    right=candidate.right,
                    status=ConflictStatus.PENDING,
                    detected_at=evaluated_at,
                )
                revision = self._detected_revision(conflict, evaluated_at)
                # Both flushes and the eventual commit share this UoW.  A UNIQUE
                # race therefore raises IntegrityError and rolls back the whole batch.
                uow.conflicts.add(conflict)
                uow.revisions.add(revision)
                created.append(conflict)
            uow.commit()
            return ConflictScan(
                evaluated_at=evaluated_at,
                dry_run=False,
                candidates=candidates,
                created=tuple(created),
                existing=existing,
            )

    def get(self, conflict_id: UUID) -> Conflict:
        """Return one conflict or raise :class:`ConflictNotFoundError`."""

        with UnitOfWork(self._session_factory) as uow:
            conflict = uow.conflicts.get(conflict_id)
            if conflict is None:
                raise ConflictNotFoundError(f"conflict {conflict_id} was not found")
            return conflict

    def list(self, *, status: ConflictStatus | None = None) -> tuple[Conflict, ...]:
        """Return conflicts in repository order, optionally filtered by current status."""

        with UnitOfWork(self._session_factory) as uow:
            return tuple(
                conflict
                for conflict in uow.conflicts.list()
                if status is None or conflict.status is status
            )

    def review(
        self,
        conflict_id: UUID,
        *,
        status: ConflictStatus,
        reason: str,
        reviewer: str | None = None,
    ) -> Conflict:
        """Record any explicit status change, including a manual reopen to pending."""

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ConflictTransitionError("a review requires a non-empty reason")

        with UnitOfWork(self._session_factory) as uow:
            current = uow.conflicts.get(conflict_id)
            if current is None:
                raise ConflictNotFoundError(f"conflict {conflict_id} was not found")
            if current.status is status:
                raise ConflictTransitionError(f"conflict {conflict_id} is already {status.value}")

            clock_time = _aware(self._clock(), "review clock")
            latest = uow.revisions.latest_for(RevisionEntityType.CONFLICT, conflict_id)
            floor = current.detected_at
            if current.reviewed_at is not None:
                floor = max(floor, current.reviewed_at)
            if latest is not None:
                floor = max(floor, latest.created_at)
            if clock_time < floor:
                raise ConflictTransitionError("review clock is earlier than conflict history")
            reviewed_at = clock_time
            normalized_reviewer = None if reviewer is None else reviewer.strip()

            updated = Conflict.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "reviewed_at": reviewed_at,
                    "review_reason": normalized_reason,
                    "reviewer": normalized_reviewer,
                }
            )
            revision = Revision(
                id=self._id_factory(),
                entity_type=RevisionEntityType.CONFLICT,
                entity_id=conflict_id,
                revision=uow.revisions.next_number(RevisionEntityType.CONFLICT, conflict_id),
                change_type=RevisionChangeType.UPDATE,
                created_at=reviewed_at,
                author=normalized_reviewer,
                payload={
                    "change": "reviewed",
                    "from_status": current.status.value,
                    "to_status": status.value,
                    "reason": normalized_reason,
                    "reviewer": normalized_reviewer,
                },
            )
            uow.conflicts.update(updated)
            uow.revisions.add(revision)
            uow.commit()
            return updated

    def history(self, conflict_id: UUID) -> tuple[Revision, ...]:
        """Return the append-only revision chain of one conflict."""

        with UnitOfWork(self._session_factory) as uow:
            if uow.conflicts.get(conflict_id) is None:
                raise ConflictNotFoundError(f"conflict {conflict_id} was not found")
            return tuple(uow.revisions.list_for(RevisionEntityType.CONFLICT, conflict_id))

    @staticmethod
    def _load_evidence(uow: UnitOfWork, evaluated_at: datetime) -> tuple[ConflictEvidence, ...]:
        evidence: list[ConflictEvidence] = []
        sources = sorted(uow.sources.list(), key=lambda source: (source.uri, str(source.id)))
        for source in sources:
            view = _load_view(uow, source.id, at=evaluated_at)
            if view.head is None:
                continue
            document = uow.documents.get(view.head.document_id)
            if document is None:
                raise RuntimeError(f"source {source.id} head references a missing document")
            states = tuple(
                state
                for unit in view.head_units
                if (state := view.state_of(unit, evaluated_at)) is not None
                and state.status in {KnowledgeUnitStatus.ACTIVE, KnowledgeUnitStatus.STALE}
            )
            context = document_service_context(state.unit.text for state in states)
            evidence.extend(
                item
                for state in states
                if (
                    item := _port_evidence(
                        source, document, state, context, section_path=state.unit.section_path
                    )
                )
                is not None
            )
        return tuple(evidence)

    @staticmethod
    def _validate_write_view(
        uow: UnitOfWork,
        candidates: tuple[ConflictCandidate, ...],
        evaluated_at: datetime,
    ) -> None:
        evidence = (item for candidate in candidates for item in (candidate.left, candidate.right))
        evidence_items = tuple(evidence)
        sources = {item.source_id for item in evidence_items}
        lineages = {item.lineage_id for item in evidence_items}
        selected = (
            (RevisionEntityType.SOURCE, entity_id) for entity_id in sorted(sources, key=str)
        )
        selected_lineages = (
            (RevisionEntityType.KNOWLEDGE_UNIT, entity_id)
            for entity_id in sorted(lineages, key=str)
        )
        for entity_type, entity_id in (*selected, *selected_lineages):
            latest = uow.revisions.latest_for(entity_type, entity_id)
            if latest is not None and latest.created_at > evaluated_at:
                moment = evaluated_at.isoformat()
                raise ValueError(
                    f"{entity_type.value} {entity_id} changed after scan time {moment}"
                )

    def _detected_revision(self, conflict: Conflict, created_at: datetime) -> Revision:
        return Revision(
            id=self._id_factory(),
            entity_type=RevisionEntityType.CONFLICT,
            entity_id=conflict.id,
            revision=1,
            change_type=RevisionChangeType.CREATE,
            created_at=created_at,
            payload={
                "change": "detected",
                "key": conflict.key,
                "rule": conflict.rule,
                "status": conflict.status.value,
                "evidence": {
                    "left": conflict.left.model_dump(mode="json"),
                    "right": conflict.right.model_dump(mode="json"),
                },
            },
        )


def _port_evidence(
    source: Source,
    document: Document,
    state: UnitState,
    document_services: frozenset[str],
    *,
    section_path: tuple[str, ...],
) -> ConflictEvidence | None:
    assertion = extract_port_assertion(
        state.unit.text, document_services=document_services, section_path=section_path
    )
    if assertion is None:
        return None
    return ConflictEvidence(
        source_id=source.id,
        document_id=document.id,
        unit_id=state.unit.id,
        lineage_id=state.unit.lineage_id,
        source_uri=source.uri,
        locator=document.locator,
        quote=state.unit.text,
        start_char=state.unit.start_char,
        end_char=state.unit.end_char,
        section_path=state.unit.section_path,
        status=state.status,
        subject=assertion.subject,
        predicate=PORT_PREDICATE,
        value=assertion.value,
    )


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include timezone information")
    return value
