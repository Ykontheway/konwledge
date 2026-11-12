"""Crash-safe opt-in automatic quality evaluation service."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session

from konwledge.auto_evaluation.models import (
    AutoEvaluationConfig,
    AutoEvaluationJob,
    AutoEvaluationStatus,
)
from konwledge.evaluation import EvaluationBaseline, GatePolicy
from konwledge.quality import QualityPolicy, QualityService
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage.auto_evaluation import (
    AutoEvaluationRepository,
    canonical_hash,
    is_knowledge_revision,
    knowledge_fingerprint,
    make_job_row,
)
from konwledge.storage.database import SessionFactory
from konwledge.storage.evaluation import EvaluationSetRepository
from konwledge.storage.tables import AutoEvaluationJobRow, RevisionRow

Clock = Callable[[], datetime]
_JOB_REVISION_IDS = "auto_evaluation_revision_ids"
_OBSERVED_JOB_IDS = "auto_evaluation_observed_job_ids"
_LEASE_DURATION = timedelta(minutes=10)


class AutoEvaluationObservation:
    """One scoped view of jobs triggered by committed domain changes.

    The observer stores only exact job identifiers created while its context is
    active. Job rows are loaded on demand, so callers never depend on history
    pagination and pre-existing failures cannot be reported again.
    """

    def __init__(self, service: AutoEvaluationService) -> None:
        self._service = service
        self._job_ids: list[UUID] = []
        self._known_ids: set[UUID] = set()
        self._token: Token[tuple[AutoEvaluationObservation, ...]] | None = None

    def __enter__(self) -> AutoEvaluationObservation:
        if self._token is not None:
            raise RuntimeError("automatic evaluation observation is already active")
        observers = self._service._observers.get()
        self._token = self._service._observers.set((*observers, self))
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        token = self._token
        if token is None:
            return
        self._token = None
        self._service._observers.reset(token)

    @property
    def job_ids(self) -> tuple[UUID, ...]:
        """Return exact committed job IDs in commit order."""

        return tuple(self._job_ids)

    @property
    def jobs(self) -> tuple[AutoEvaluationJob, ...]:
        """Load the current durable state of exactly the observed jobs."""

        return self._service._load_jobs(self._job_ids)

    def _record(self, job_ids: tuple[UUID, ...]) -> None:
        for job_id in job_ids:
            if job_id not in self._known_ids:
                self._known_ids.add(job_id)
                self._job_ids.append(job_id)


class AutoEvaluationError(ValueError):
    """Safe caller-facing automatic evaluation configuration error."""


class AutoEvaluationNotFoundError(LookupError):
    """Raised when an automatic evaluation job or dataset pin does not exist."""


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutoEvaluationError(f"{name} must be timezone-aware")
    return value


def _utc_iso(value: datetime) -> str:
    return _aware(value, "timestamp").astimezone(UTC).isoformat()


def _strict_positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AutoEvaluationError(f"{name} must be a positive integer")
    return value


class AutoEvaluationService:
    """Persist configuration, capture committed revisions, and drain durable jobs.

    One service is registered on each session factory. Revision hooks only enqueue on
    an outer commit; SAVEPOINT commits merge their revision IDs into the parent and
    SAVEPOINT rollbacks discard only their own IDs.
    """

    def __new__(
        cls, session_factory: SessionFactory, *, clock: Clock | None = None
    ) -> AutoEvaluationService:
        existing = getattr(session_factory, "_auto_evaluation_service", None)
        if isinstance(existing, cls):
            if clock is not None:
                existing._replace_clock(clock)
            return existing
        instance = super().__new__(cls)
        session_factory._auto_evaluation_service = instance  # type: ignore[attr-defined]
        return instance

    def __init__(self, session_factory: SessionFactory, *, clock: Clock | None = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._replace_clock(self._clock)
        self._draining = False
        self._observers: ContextVar[tuple[AutoEvaluationObservation, ...]] = ContextVar(
            f"auto_evaluation_observers_{id(self)}", default=()
        )
        event.listen(session_factory, "after_transaction_create", self._after_transaction_create)
        event.listen(session_factory, "after_flush", self._after_flush)
        event.listen(session_factory, "before_commit", self._before_commit)
        event.listen(session_factory, "after_commit", self._after_commit)
        event.listen(session_factory, "after_rollback", self._after_rollback)

    def _replace_clock(self, clock: Clock) -> None:
        self._clock = clock
        self._retrieval = RetrievalService(self._session_factory, clock=clock)
        self._quality = QualityService(self._session_factory, self._retrieval, clock=clock)

    def observe_changes(self) -> AutoEvaluationObservation:
        """Observe only automatic jobs triggered by domain commits in this scope.

        Configuration, manual run, retry, and read operations are intentionally
        excluded because they do not create jobs through the revision hook.
        """

        return AutoEvaluationObservation(self)

    def watch_changes(self) -> AutoEvaluationObservation:
        """Compatibility alias for :meth:`observe_changes`."""

        return self.observe_changes()

    def _load_jobs(self, job_ids: list[UUID]) -> tuple[AutoEvaluationJob, ...]:
        if not job_ids:
            return ()
        with self._session_factory() as session:
            repository = AutoEvaluationRepository(session)
            jobs: list[AutoEvaluationJob] = []
            for job_id in job_ids:
                job = repository.get_job(job_id)
                if job is None:  # pragma: no cover - durable rows are never deleted
                    raise RuntimeError(f"observed automatic evaluation job {job_id} disappeared")
                jobs.append(job)
            return tuple(jobs)

    def enable(
        self,
        dataset_id: UUID,
        *,
        version: int,
        policy: QualityPolicy | None = None,
        baseline: EvaluationBaseline | None = None,
        regression_policy: GatePolicy | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
    ) -> AutoEvaluationConfig:
        """Validate and persist an exact dataset/configuration pin without running it."""

        if not isinstance(dataset_id, UUID):
            raise AutoEvaluationError("dataset_id must be a UUID")
        version = _strict_positive_int(version, "version")
        top_k = _strict_positive_int(top_k, "top_k")
        rrf_k = _strict_positive_int(rrf_k, "rrf_k")
        if candidate_limit is not None:
            candidate_limit = _strict_positive_int(candidate_limit, "candidate_limit")
        if not isinstance(mode, RetrievalMode):
            raise AutoEvaluationError("mode must be a RetrievalMode")
        if mode is not RetrievalMode.LEXICAL:
            raise AutoEvaluationError(
                "automatic vector or hybrid evaluation requires a persistent configured provider"
            )

        now = _aware(self._clock(), "clock")
        selected_policy = (
            QualityPolicy() if policy is None else QualityPolicy.model_validate(policy, strict=True)
        )
        with self._session_factory() as session:
            dataset = EvaluationSetRepository(session).get(dataset_id, version)
            if dataset is None:
                raise AutoEvaluationNotFoundError(
                    f"evaluation set {dataset_id} version {version} was not found"
                )
            repository = AutoEvaluationRepository(session)
            previous = repository.config()
            generation = 1 if previous is None else previous.generation + 1
            config = AutoEvaluationConfig(
                generation=generation,
                enabled=True,
                dataset_id=dataset_id,
                dataset_version=version,
                dataset_hash=dataset.content_hash,
                quality_policy=selected_policy,
                baseline=baseline,
                regression_policy=regression_policy,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                updated_at=now,
            )
            repository.put_config(config)
            session.commit()
            return config

    def disable(self) -> AutoEvaluationConfig | None:
        now = _aware(self._clock(), "clock")
        with self._session_factory() as session:
            repository = AutoEvaluationRepository(session)
            previous = repository.config()
            if previous is None:
                return None
            if not previous.enabled:
                return previous
            disabled = AutoEvaluationConfig.model_validate(
                previous.model_copy(
                    update={
                        "generation": previous.generation + 1,
                        "enabled": False,
                        "updated_at": now,
                    }
                ).model_dump()
            )
            repository.put_config(disabled)
            session.commit()
            return disabled

    def status(self) -> AutoEvaluationConfig | None:
        with self._session_factory() as session:
            return AutoEvaluationRepository(session).config()

    def enqueue(self) -> AutoEvaluationJob:
        """Append a manual run from the current pin without changing a baseline."""

        now = _aware(self._clock(), "clock")
        with self._session_factory() as session:
            repository = AutoEvaluationRepository(session)
            pinned = repository.config_with_fingerprint()
            if pinned is None or not pinned[0].enabled:
                raise AutoEvaluationError("automatic evaluation is disabled")
            config, fingerprint = pinned
            row = make_job_row(
                job_id=uuid4(),
                config=config,
                fingerprint=fingerprint,
                knowledge=knowledge_fingerprint(session),
                revisions=(),
                now=now,
            )
            repository.add_job_row(row)
            session.commit()
            job = repository.get_job(row.id)
            if job is None:  # pragma: no cover - committed primary key
                raise RuntimeError("automatic evaluation job disappeared")
            return job

    def run(self) -> AutoEvaluationJob:
        job = self.enqueue()
        self.drain()
        return self.show(job.id)

    def drain(self, *, limit: int | None = None) -> tuple[AutoEvaluationJob, ...]:
        """Claim and execute queued jobs. Claim/finalize writes use lease-token CAS."""

        if self._draining:
            return ()
        if limit is not None:
            limit = _strict_positive_int(limit, "limit")
        self._draining = True
        completed: list[AutoEvaluationJob] = []
        try:
            while limit is None or len(completed) < limit:
                claimed = self._claim_one()
                if claimed is None:
                    break
                job_id, lease_token = claimed
                self._execute(job_id, lease_token)
                completed.append(self.show(job_id))
        finally:
            self._draining = False
        return tuple(completed)

    def recover(self) -> int:
        """Requeue jobs whose persisted UTC lease has expired.

        Each claim increments ``attempt_count``. Recovery appends a privacy-safe
        symbolic record and invalidates the old token, so a late worker cannot write.
        """

        now = _aware(self._clock(), "clock")
        now_iso = _utc_iso(now)
        with self._session_factory() as session:
            expired = session.scalars(
                select(AutoEvaluationJobRow).where(
                    AutoEvaluationJobRow.status == AutoEvaluationStatus.RUNNING.value,
                    AutoEvaluationJobRow.lease_expires_at.is_not(None),
                    AutoEvaluationJobRow.lease_expires_at < now_iso,
                )
            ).all()
            recovered = 0
            for row in expired:
                token = row.lease_token
                if token is None:
                    continue
                errors = [*row.recovery_errors, {"at": now_iso, "code": "LEASE_EXPIRED"}]
                result = session.execute(
                    update(AutoEvaluationJobRow)
                    .where(
                        AutoEvaluationJobRow.id == row.id,
                        AutoEvaluationJobRow.status == AutoEvaluationStatus.RUNNING.value,
                        AutoEvaluationJobRow.lease_token == token,
                    )
                    .values(
                        status=AutoEvaluationStatus.QUEUED.value,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code="LEASE_EXPIRED",
                        recovery_errors=errors,
                    )
                )
                recovered += int(getattr(result, "rowcount", 0) == 1)
            session.commit()
            return recovered

    def retry(self, job_id: UUID) -> AutoEvaluationJob:
        now = _aware(self._clock(), "clock")
        with self._session_factory() as session:
            repository = AutoEvaluationRepository(session)
            original = repository.get_job(job_id)
            if original is None:
                raise AutoEvaluationNotFoundError(
                    f"automatic evaluation job {job_id} was not found"
                )
            if original.status not in {
                AutoEvaluationStatus.FAILED,
                AutoEvaluationStatus.BLOCKED,
                AutoEvaluationStatus.SUPERSEDED,
            }:
                raise AutoEvaluationError("only terminal unsuccessful jobs can be retried")
            pinned = repository.config_with_fingerprint()
            if pinned is None or not pinned[0].enabled:
                raise AutoEvaluationError("automatic evaluation is disabled")
            config, fingerprint = pinned
            row = make_job_row(
                job_id=uuid4(),
                parent_job_id=job_id,
                config=config,
                fingerprint=fingerprint,
                knowledge=knowledge_fingerprint(session),
                revisions=(),
                now=now,
            )
            repository.add_job_row(row)
            session.commit()
            retried = repository.get_job(row.id)
            if retried is None:  # pragma: no cover - committed primary key
                raise RuntimeError("automatic evaluation retry disappeared")
            return retried

    def history(self, *, limit: int = 100) -> tuple[AutoEvaluationJob, ...]:
        limit = _strict_positive_int(limit, "limit")
        with self._session_factory() as session:
            return tuple(AutoEvaluationRepository(session).history(limit=limit))

    def show(self, job_id: UUID) -> AutoEvaluationJob:
        with self._session_factory() as session:
            job = AutoEvaluationRepository(session).get_job(job_id)
        if job is None:
            raise AutoEvaluationNotFoundError(f"automatic evaluation job {job_id} was not found")
        return job

    def _after_transaction_create(self, session: Session, transaction: Any) -> None:
        if transaction.nested:
            stack = session.info.setdefault(_JOB_REVISION_IDS, [set()])
            stack.append(set())

    def _after_flush(self, session: Session, _context: object) -> None:
        stack = session.info.setdefault(_JOB_REVISION_IDS, [set()])
        for instance in session.new:
            if isinstance(instance, RevisionRow) and is_knowledge_revision(instance):
                stack[-1].add(instance.id)

    def _before_commit(self, session: Session) -> None:
        if session.in_nested_transaction():
            return
        # ``commit()`` normally flushes after this event. Flush explicitly so direct
        # ``session.add(RevisionRow(...)); session.commit()`` is observed here.
        session.flush()
        stack = session.info.get(_JOB_REVISION_IDS, ())
        revision_ids = tuple(sorted({item for level in stack for item in level}, key=str))
        if not revision_ids:
            return
        repository = AutoEvaluationRepository(session)
        pinned = repository.config_with_fingerprint()
        if pinned is None or not pinned[0].enabled:
            return
        config, fingerprint = pinned
        now = _aware(self._clock(), "clock")
        revision_digest = canonical_hash([str(value) for value in revision_ids])
        job_id = uuid4()
        session.add(
            make_job_row(
                job_id=job_id,
                config=config,
                fingerprint=fingerprint,
                knowledge=knowledge_fingerprint(session),
                revisions=revision_ids,
                now=now,
                trigger_key=f"{config.generation}:{revision_digest}",
            )
        )
        if self._observers.get():
            session.info.setdefault(_OBSERVED_JOB_IDS, []).append(job_id)

    def _after_commit(self, session: Session) -> None:
        stack = session.info.get(_JOB_REVISION_IDS)
        if session.in_nested_transaction():
            if stack and len(stack) > 1:
                nested = stack.pop()
                stack[-1].update(nested)
            return
        had_revisions = bool(stack and any(stack))
        session.info.pop(_JOB_REVISION_IDS, None)
        observed_job_ids = tuple(session.info.pop(_OBSERVED_JOB_IDS, ()))
        if observed_job_ids:
            for observer in self._observers.get():
                observer._record(observed_job_ids)
        if not had_revisions or self._draining:
            return
        try:
            self.drain()
        except Exception:
            # The knowledge commit succeeded already. Keep durable audit state and
            # never turn a worker/provider error into a false ingestion failure.
            return

    @staticmethod
    def _after_rollback(session: Session) -> None:
        stack = session.info.get(_JOB_REVISION_IDS)
        if session.in_nested_transaction():
            if stack and len(stack) > 1:
                stack.pop()
            return
        session.info.pop(_JOB_REVISION_IDS, None)
        session.info.pop(_OBSERVED_JOB_IDS, None)

    def _claim_one(self) -> tuple[UUID, UUID] | None:
        now = _aware(self._clock(), "clock")
        token = uuid4()
        with self._session_factory() as session:
            candidate = session.scalar(
                select(AutoEvaluationJobRow.id)
                .where(AutoEvaluationJobRow.status == AutoEvaluationStatus.QUEUED.value)
                .order_by(AutoEvaluationJobRow.created_at, AutoEvaluationJobRow.id)
                .limit(1)
            )
            if candidate is None:
                return None
            claimed = session.execute(
                update(AutoEvaluationJobRow)
                .where(
                    AutoEvaluationJobRow.id == candidate,
                    AutoEvaluationJobRow.status == AutoEvaluationStatus.QUEUED.value,
                )
                .values(
                    status=AutoEvaluationStatus.RUNNING.value,
                    started_at=_utc_iso(now),
                    lease_token=token,
                    lease_expires_at=_utc_iso(now + _LEASE_DURATION),
                    error_code=None,
                    attempt_count=AutoEvaluationJobRow.attempt_count + 1,
                )
            )
            session.commit()
            if getattr(claimed, "rowcount", 0) != 1:
                return None
            return candidate, token

    def _execute(self, job_id: UUID, token: UUID) -> None:
        job = self.show(job_id)
        preflight = self._preflight(job)
        if isinstance(preflight, tuple):
            self._finalize(job_id, token, preflight[0], error_code=preflight[1])
            return
        config = preflight
        try:
            report = self._quality.run(
                config.dataset_id,
                version=config.dataset_version,
                policy=config.quality_policy,
                baseline=config.baseline,
                regression_policy=config.regression_policy,
                mode=config.mode,
                top_k=config.top_k,
                candidate_limit=config.candidate_limit,
                rrf_k=config.rrf_k,
                at=job.evaluated_at,
            )
            compact = {
                "passed": report.passed,
                "dataset": {
                    "id": str(job.dataset_id),
                    "version": job.dataset_version,
                    "hash": job.dataset_hash,
                },
                "metrics": report.metrics.model_dump(mode="json"),
                "citation_coverage": report.citation_coverage,
                "stale_hits": report.stale_hits,
                "degraded": report.degraded,
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "measured": check.measured,
                        "required": check.required,
                    }
                    for check in report.checks
                ],
            }
            status = AutoEvaluationStatus.PASSED if report.passed else AutoEvaluationStatus.FAILED
            code = None if report.passed else "QUALITY_GATE_FAILED"
            self._finalize(job_id, token, status, report=compact, error_code=code)
        except (LookupError, ValidationError, ValueError):
            self._finalize(
                job_id,
                token,
                AutoEvaluationStatus.FAILED,
                error_code="INVALID_EVALUATION",
            )
        except Exception:
            self._finalize(
                job_id,
                token,
                AutoEvaluationStatus.FAILED,
                error_code="EVALUATION_ERROR",
            )

    def _preflight(
        self, job: AutoEvaluationJob
    ) -> AutoEvaluationConfig | tuple[AutoEvaluationStatus, str]:
        with self._session_factory() as session:
            repository = AutoEvaluationRepository(session)
            try:
                pinned = repository.config_with_fingerprint()
                if pinned is None:
                    return AutoEvaluationStatus.SUPERSEDED, "CONFIG_MISSING"
                config, fingerprint = pinned
                dataset_hash = repository.get_dataset_hash(job.dataset_id, job.dataset_version)
            except (TypeError, ValueError):
                return AutoEvaluationStatus.BLOCKED, "INTEGRITY_MISMATCH"
            if not config.enabled:
                return AutoEvaluationStatus.SUPERSEDED, "CONFIG_DISABLED"
            if config.generation != job.config_generation or fingerprint != job.config_fingerprint:
                return AutoEvaluationStatus.SUPERSEDED, "CONFIG_CHANGED"
            if dataset_hash != job.dataset_hash:
                return AutoEvaluationStatus.BLOCKED, "DATASET_PIN_MISMATCH"
            if knowledge_fingerprint(session) != job.knowledge_fingerprint:
                return AutoEvaluationStatus.SUPERSEDED, "KNOWLEDGE_CHANGED"
            return config

    def _finalize(
        self,
        job_id: UUID,
        token: UUID,
        status: AutoEvaluationStatus,
        *,
        report: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        """Verify pins and lease-CAS the result in one BEGIN IMMEDIATE transaction."""

        now = _aware(self._clock(), "clock")
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(AutoEvaluationJobRow, job_id)
            if (
                row is None
                or row.status != AutoEvaluationStatus.RUNNING.value
                or row.lease_token != token
            ):
                session.rollback()
                return False

            repository = AutoEvaluationRepository(session)
            try:
                pinned = repository.config_with_fingerprint()
                if pinned is None:
                    status, error_code = AutoEvaluationStatus.SUPERSEDED, "CONFIG_MISSING"
                else:
                    config, fingerprint = pinned
                    if not config.enabled:
                        status, error_code = AutoEvaluationStatus.SUPERSEDED, "CONFIG_DISABLED"
                    elif (
                        config.generation != row.config_generation
                        or fingerprint != row.config_fingerprint
                    ):
                        status, error_code = AutoEvaluationStatus.SUPERSEDED, "CONFIG_CHANGED"
                    elif (
                        repository.get_dataset_hash(row.dataset_id, row.dataset_version)
                        != row.dataset_hash
                    ):
                        status, error_code = (
                            AutoEvaluationStatus.BLOCKED,
                            "DATASET_PIN_MISMATCH",
                        )
                    elif knowledge_fingerprint(session) != row.knowledge_fingerprint:
                        status, error_code = AutoEvaluationStatus.SUPERSEDED, "KNOWLEDGE_CHANGED"
            except (TypeError, ValueError):
                status, error_code = AutoEvaluationStatus.BLOCKED, "INTEGRITY_MISMATCH"

            result = session.execute(
                update(AutoEvaluationJobRow)
                .where(
                    AutoEvaluationJobRow.id == job_id,
                    AutoEvaluationJobRow.status == AutoEvaluationStatus.RUNNING.value,
                    AutoEvaluationJobRow.lease_token == token,
                )
                .values(
                    status=status.value,
                    finished_at=_utc_iso(now),
                    lease_token=None,
                    lease_expires_at=None,
                    report=report,
                    error_code=error_code,
                )
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0) == 1)
