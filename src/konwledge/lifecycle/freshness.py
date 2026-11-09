"""Append-only freshness policies and deterministic stale scans."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from konwledge.domain import (
    FreshnessPolicy,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
)
from konwledge.lifecycle.service import _load_view, materialize_statuses
from konwledge.lifecycle.status import REFRESHED
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]
_POLICY_CHANGE = "freshness_policy"
_TTL_CHANGES = frozenset({"added", "modified", REFRESHED})


class SourceNotFoundError(LookupError):
    """Raised when a freshness operation names an unknown source."""


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    """The freshness evaluation of one currently published unit."""

    unit_id: UUID
    lineage_id: UUID
    source_id: UUID
    status: KnowledgeUnitStatus
    checked_at: datetime
    reference_at: datetime | None
    expires_at: datetime | None
    reason_codes: tuple[str, ...]
    reason: str | None

    @property
    def expired(self) -> bool:
        return bool(self.reason_codes)


@dataclass(frozen=True, slots=True)
class FreshnessScan:
    """A stable report plus revisions appended by an applying scan."""

    evaluated_at: datetime
    dry_run: bool
    decisions: tuple[FreshnessDecision, ...]
    marked: tuple[Revision, ...]


class FreshnessService:
    """Configure source policies and evaluate current or historical heads."""

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

    def configure(self, source_id: UUID, policy: FreshnessPolicy) -> Revision:
        """Append a policy revision; an empty policy explicitly disables scanning."""

        created_at = _aware(self._clock(), "clock")
        with UnitOfWork(self._session_factory) as uow:
            if uow.sources.get(source_id) is None:
                raise SourceNotFoundError(f"source {source_id} was not found")
            source_chain = uow.revisions.list_for(RevisionEntityType.SOURCE, source_id)
            _ensure_not_backdated(created_at, source_chain)
            number = uow.revisions.next_number(RevisionEntityType.SOURCE, source_id)
            revision = Revision(
                id=self._id_factory(),
                entity_type=RevisionEntityType.SOURCE,
                entity_id=source_id,
                revision=number,
                change_type=(
                    RevisionChangeType.CREATE if number == 1 else RevisionChangeType.UPDATE
                ),
                created_at=created_at,
                payload={
                    "change": _POLICY_CHANGE,
                    "policy": policy.model_dump(mode="json"),
                },
            )
            uow.revisions.add(revision)
            uow.commit()
            return revision

    def policy(self, source_id: UUID, at: datetime | None = None) -> FreshnessPolicy | None:
        """Return the latest configured policy visible at ``at``."""

        point = None if at is None else _aware(at, "at")
        with UnitOfWork(self._session_factory) as uow:
            if uow.sources.get(source_id) is None:
                raise SourceNotFoundError(f"source {source_id} was not found")
            return _policy_at(uow.revisions.list_for(RevisionEntityType.SOURCE, source_id), point)

    def scan(
        self,
        *,
        at: datetime | None = None,
        dry_run: bool = True,
        source_id: UUID | None = None,
    ) -> FreshnessScan:
        """Evaluate source heads and atomically append stale marks when requested."""

        if not dry_run and at is not None:
            raise ValueError("historical freshness scans are read-only")
        evaluated_at = _aware(at if at is not None else self._clock(), "at")
        with UnitOfWork(self._session_factory) as uow:
            if source_id is not None and uow.sources.get(source_id) is None:
                raise SourceNotFoundError(f"source {source_id} was not found")
            sources = sorted(
                (
                    source
                    for source in uow.sources.list()
                    if source_id is None or source.id == source_id
                ),
                key=lambda source: (source.uri, str(source.id)),
            )
            decisions: list[FreshnessDecision] = []
            marked: list[Revision] = []
            touched_sources: set[UUID] = set()
            for source in sources:
                source_chain = uow.revisions.list_for(RevisionEntityType.SOURCE, source.id)
                policy = _policy_at(source_chain, evaluated_at)
                if policy is None or _disabled(policy):
                    continue
                view = _load_view(uow, source.id, at=evaluated_at)
                if view.head is None:
                    continue
                if not dry_run:
                    _ensure_not_backdated(evaluated_at, source_chain)
                units = sorted(view.head_units, key=lambda unit: (unit.ordinal, str(unit.id)))
                for unit in units:
                    state = view.state_of(unit, evaluated_at)
                    if state is None or state.status not in (
                        KnowledgeUnitStatus.ACTIVE,
                        KnowledgeUnitStatus.STALE,
                    ):
                        continue
                    chain = uow.revisions.list_for(
                        RevisionEntityType.KNOWLEDGE_UNIT, unit.lineage_id
                    )
                    if not dry_run:
                        _ensure_not_backdated(evaluated_at, chain)
                    decision = _evaluate(
                        unit,
                        source.id,
                        state.status,
                        policy,
                        chain,
                        head_published_at=view.head.published_at,
                        checked_at=evaluated_at,
                    )
                    decisions.append(decision)
                    if (
                        not dry_run
                        and decision.expired
                        and state.status is not KnowledgeUnitStatus.STALE
                    ):
                        revision = _stale_revision(
                            uow=uow,
                            unit=unit,
                            decision=decision,
                            policy=policy,
                            id_factory=self._id_factory,
                        )
                        uow.revisions.add(revision)
                        marked.append(revision)
                        touched_sources.add(source.id)
            if not dry_run:
                for touched in sorted(touched_sources, key=str):
                    materialize_statuses(uow, touched)
                uow.commit()
            return FreshnessScan(
                evaluated_at=evaluated_at,
                dry_run=dry_run,
                decisions=tuple(decisions),
                marked=tuple(marked),
            )


def _policy_at(chain: Sequence[Revision], at: datetime | None) -> FreshnessPolicy | None:
    configured = [
        revision
        for revision in chain
        if revision.payload.get("change") == _POLICY_CHANGE
        and (at is None or revision.created_at <= at)
    ]
    if not configured:
        return None
    latest = max(configured, key=lambda revision: revision.revision)
    raw = latest.payload.get("policy")
    if not isinstance(raw, dict):
        raise ValueError("stored freshness policy is not an object")
    return FreshnessPolicy.model_validate(raw)


def _disabled(policy: FreshnessPolicy) -> bool:
    return policy.ttl_seconds is None and policy.source_updated_at is None


def _evaluate(
    unit: KnowledgeUnit,
    source_id: UUID,
    status: KnowledgeUnitStatus,
    policy: FreshnessPolicy,
    chain: Sequence[Revision],
    *,
    head_published_at: datetime,
    checked_at: datetime,
) -> FreshnessDecision:
    visible = [revision for revision in chain if revision.created_at <= checked_at]
    ttl_events = [
        revision for revision in visible if revision.payload.get("change") in _TTL_CHANGES
    ]
    reference_at = (
        max(ttl_events, key=lambda revision: (revision.created_at, revision.revision)).created_at
        if ttl_events
        else head_published_at
    )
    expires_at = None
    if policy.ttl_seconds is not None:
        try:
            # TTL is elapsed seconds, not local wall-clock days across DST changes.
            expires_at = reference_at.astimezone(UTC) + timedelta(seconds=policy.ttl_seconds)
        except OverflowError as error:
            raise ValueError("TTL deadline exceeds datetime's supported range") from error
    refreshed = [revision for revision in visible if revision.payload.get("change") == REFRESHED]
    source_reference = max([head_published_at, *(revision.created_at for revision in refreshed)])
    reasons: list[str] = []
    if expires_at is not None and checked_at >= expires_at:
        reasons.append("ttl_expired")
    if (
        policy.source_updated_at is not None
        and source_reference < policy.source_updated_at <= checked_at
    ):
        reasons.append("source_updated")
    reason_codes = tuple(reasons)
    return FreshnessDecision(
        unit_id=unit.id,
        lineage_id=unit.lineage_id,
        source_id=source_id,
        status=status,
        checked_at=checked_at,
        reference_at=reference_at,
        expires_at=expires_at,
        reason_codes=reason_codes,
        reason=",".join(reason_codes) or None,
    )


def _stale_revision(
    *,
    uow: UnitOfWork,
    unit: KnowledgeUnit,
    decision: FreshnessDecision,
    policy: FreshnessPolicy,
    id_factory: IdFactory,
) -> Revision:
    return Revision(
        id=id_factory(),
        entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
        entity_id=unit.lineage_id,
        revision=uow.revisions.next_number(RevisionEntityType.KNOWLEDGE_UNIT, unit.lineage_id),
        change_type=RevisionChangeType.UPDATE,
        created_at=decision.checked_at,
        payload={
            "change": "stale",
            "unit_id": str(unit.id),
            "document_id": str(unit.document_id),
            "content_hash": unit.content_hash,
            "reason": decision.reason,
            "freshness": {
                "reason_codes": list(decision.reason_codes),
                "reference_at": (
                    decision.reference_at.isoformat() if decision.reference_at is not None else None
                ),
                "expires_at": (
                    decision.expires_at.isoformat() if decision.expires_at is not None else None
                ),
                "policy": policy.model_dump(mode="json"),
            },
        },
    )


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include timezone information")
    return value


def _ensure_not_backdated(now: datetime, revisions: Sequence[Revision]) -> None:
    latest = max((revision.created_at for revision in revisions), default=None)
    if latest is not None and latest > now:
        raise ValueError("clock is earlier than existing revision history")
