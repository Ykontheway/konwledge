"""U12 conflict regressions for extraction, historical replay, and rollback safety."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from konwledge.conflicts import ConflictService
from konwledge.conflicts.rules import extract_port_assertion
from konwledge.domain import (
    Conflict,
    ConflictStatus,
    Document,
    DocumentContentType,
    KnowledgeUnit,
    Source,
    SourceKind,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.repository import RevisionRepository

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T1 + timedelta(hours=1)
T3 = T2 + timedelta(hours=1)
SEEDED_AT = T0 - timedelta(minutes=5)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


def _checksum(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _add_fact(
    uow: UnitOfWork,
    text: str,
    *,
    name: str,
    source_id: UUID | None = None,
    document_id: UUID | None = None,
    unit_id: UUID | None = None,
    lineage_id: UUID | None = None,
    ingested_at: datetime = SEEDED_AT,
    ordinal: int = 0,
    section_path: tuple[str, ...] = ("Ports",),
    source_uri: str | None = None,
) -> tuple[UUID, UUID, UUID, UUID]:
    sid = source_id or uuid4()
    did = document_id or uuid4()
    uid = unit_id or uuid4()
    lineage = uid if lineage_id is None else lineage_id
    locator = f"{name}.md"
    if uow.sources.get(sid) is None:
        uow.sources.add(
            Source(
                id=sid,
                kind=SourceKind.FILE,
                uri=source_uri or locator,
                title=name,
                created_at=ingested_at,
            )
        )
    uow.documents.add(
        Document(
            id=did,
            source_id=sid,
            locator=locator,
            content_type=DocumentContentType.MARKDOWN,
            checksum=_checksum(f"{name}:{did}"),
            ingested_at=ingested_at,
        )
    )
    uow.knowledge_units.add(
        KnowledgeUnit(
            id=uid,
            lineage_id=lineage,
            document_id=did,
            ordinal=ordinal,
            text=text,
            start_char=0,
            end_char=len(text),
            section_path=section_path,
        )
    )
    return sid, did, uid, lineage


def _add_pair(
    session_factory: SessionFactory,
    *,
    left: str = "order-service listens on port 8000",
    right: str = "order-service listens on port 8080",
    ingested_at: datetime = SEEDED_AT,
) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID, UUID, UUID]:
    with UnitOfWork(session_factory) as uow:
        left_ids = _add_fact(uow, left, name="left", ingested_at=ingested_at)
        right_ids = _add_fact(uow, right, name="right", ingested_at=ingested_at)
        uow.commit()
    return (*left_ids, *right_ids)


def _scan_existing_at(session_factory: SessionFactory, at: datetime) -> tuple[Conflict, ...]:
    return ConflictService(session_factory, clock=lambda: at).scan(at=at).existing


def test_accepts_deterministic_port_when_healthz_http_url_and_port_env_coexist() -> None:
    assertion = extract_port_assertion(
        "order-service listens on port 8080; "
        "/healthz is available at http://127.0.0.1:8080/healthz; "
        "export PORT=8080 before startup."
    )

    assert assertion is not None
    assert (assertion.subject, assertion.value) == ("order-service", "8080")


@pytest.mark.parametrize(
    "text",
    [
        "order-service listens on port 8000/8080",
        "order-service 监听 8000 或 9000 端口",
        "order-service config: port=8000/8080",
        "order-service config: port=123456",
        "order-service config: do not use port=8000",
    ],
)
def test_rejects_ambiguous_invalid_and_negated_port_forms(text: str) -> None:
    assert extract_port_assertion(text) is None


def test_section_path_context_is_strict_and_local_subject_remains_authoritative() -> None:
    path_only = extract_port_assertion(
        "默认监听端口 8000",
        section_path=("order-service", "Deployment"),
    )
    assert path_only is not None
    assert (path_only.subject, path_only.value) == ("order-service", "8000")

    assert (
        extract_port_assertion(
            "默认监听端口 8000",
            section_path=("order-service", "user-service"),
            document_services=frozenset({"order-service"}),
        )
        is None
    )

    local_subject = extract_port_assertion(
        "payment-service listens on port 8000",
        section_path=("order-service", "Deployment"),
        document_services=frozenset({"order-service"}),
    )
    assert local_subject is not None
    assert (local_subject.subject, local_subject.value) == ("payment-service", "8000")


def test_historical_scan_replays_review_state_without_future_leaks(
    session_factory: SessionFactory,
) -> None:
    _add_pair(session_factory)
    detected_service = ConflictService(session_factory, clock=lambda: T0)
    [detected] = detected_service.scan(dry_run=False).created

    confirmed = ConflictService(session_factory, clock=lambda: T1).review(
        detected.id,
        status=ConflictStatus.CONFIRMED,
        reason="confirmed by reviewer at T1",
        reviewer="alice@example.com",
    )
    resolved = ConflictService(session_factory, clock=lambda: T2).review(
        detected.id,
        status=ConflictStatus.RESOLVED,
        reason="resolved by reviewer at T2",
        reviewer="bob@example.com",
    )
    reopened = ConflictService(session_factory, clock=lambda: T3).review(
        detected.id,
        status=ConflictStatus.PENDING,
        reason="reopened at T3",
        reviewer="carol@example.com",
    )

    assert detected.reviewed_at is None
    assert detected.review_reason is None
    assert detected.reviewer is None
    assert _scan_existing_at(session_factory, T0 - timedelta(seconds=1)) == ()
    assert _scan_existing_at(session_factory, T0) == (detected,)
    assert _scan_existing_at(session_factory, T0 + timedelta(minutes=30)) == (detected,)
    assert _scan_existing_at(session_factory, T1) == (confirmed,)
    assert _scan_existing_at(session_factory, T1 + timedelta(minutes=30)) == (confirmed,)
    assert _scan_existing_at(session_factory, T2) == (resolved,)
    assert _scan_existing_at(session_factory, T3) == (reopened,)


def test_same_timestamp_reviews_pick_latest_revision_number_without_future_leaks(
    session_factory: SessionFactory,
) -> None:
    _add_pair(session_factory)
    [detected] = ConflictService(session_factory, clock=lambda: T0).scan(dry_run=False).created

    reviewer_at_t1 = ConflictService(session_factory, clock=lambda: T1)
    reviewer_at_t1.review(
        detected.id,
        status=ConflictStatus.CONFIRMED,
        reason="first decision at shared timestamp",
        reviewer="alice@example.com",
    )
    resolved_same_time = reviewer_at_t1.review(
        detected.id,
        status=ConflictStatus.RESOLVED,
        reason="second decision at shared timestamp",
        reviewer="bob@example.com",
    )
    future_reopen = ConflictService(session_factory, clock=lambda: T2).review(
        detected.id,
        status=ConflictStatus.PENDING,
        reason="future reopen should stay invisible",
        reviewer="carol@example.com",
    )

    [historical] = _scan_existing_at(session_factory, T1)
    assert historical == resolved_same_time
    assert historical.review_reason == "second decision at shared timestamp"
    assert historical.reviewer == "bob@example.com"
    assert historical.review_reason != future_reopen.review_reason
    assert historical.reviewer != future_reopen.reviewer


def test_changed_port_value_creates_new_key_and_keeps_old_conflict_out_of_current_candidates(
    session_factory: SessionFactory,
) -> None:
    left_source, _, _, _, right_source, _, _, right_lineage = _add_pair(session_factory)
    [original] = ConflictService(session_factory, clock=lambda: T0).scan(dry_run=False).created

    with UnitOfWork(session_factory) as uow:
        _add_fact(
            uow,
            "order-service listens on port 9000",
            name="right-v2",
            source_id=right_source,
            lineage_id=right_lineage,
            ingested_at=T2,
        )
        uow.commit()

    preview = ConflictService(session_factory, clock=lambda: T2).scan()
    [candidate] = preview.candidates
    assert left_source in {candidate.left.source_id, candidate.right.source_id}
    assert {candidate.left.value, candidate.right.value} == {"8000", "9000"}
    assert candidate.key != original.key
    assert all(item.key != original.key for item in preview.candidates)

    [new_conflict] = ConflictService(session_factory, clock=lambda: T2).scan(dry_run=False).created
    assert new_conflict.key == candidate.key

    stored = ConflictService(session_factory, clock=lambda: T2).list()
    assert {conflict.key for conflict in stored} == {original.key, new_conflict.key}
    assert ConflictService(session_factory, clock=lambda: T2).get(original.id) == original

    rerun = ConflictService(session_factory, clock=lambda: T2).scan(dry_run=False)
    assert rerun.created == ()
    assert rerun.existing == (new_conflict,)


def test_scan_is_read_only_for_current_conflict_state_and_revision_chain(
    session_factory: SessionFactory,
) -> None:
    _add_pair(session_factory)
    [detected] = ConflictService(session_factory, clock=lambda: T0).scan(dry_run=False).created
    reviewed = ConflictService(session_factory, clock=lambda: T1).review(
        detected.id,
        status=ConflictStatus.CONFIRMED,
        reason="confirmed before preview scans",
        reviewer="alice@example.com",
    )

    current_service = ConflictService(session_factory, clock=lambda: T3)
    before = current_service.get(detected.id)
    before_history = current_service.history(detected.id)

    preview = current_service.scan()
    historical = current_service.scan(at=T0)

    after = current_service.get(detected.id)
    after_history = current_service.history(detected.id)

    assert preview.dry_run is True
    assert historical.dry_run is True
    assert historical.existing == (detected,)
    assert before == after == reviewed
    assert before_history == after_history


def test_review_rolls_back_when_revision_write_fails(
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: T0)
    [detected] = service.scan(dry_run=False).created
    history_before = service.history(detected.id)

    def fail_add(self: RevisionRepository, entity: object) -> None:
        raise RuntimeError("synthetic revision write failure")

    monkeypatch.setattr(RevisionRepository, "add", fail_add)

    failing_service = ConflictService(session_factory, clock=lambda: T1)
    with pytest.raises(RuntimeError, match="synthetic revision write failure"):
        failing_service.review(
            detected.id,
            status=ConflictStatus.CONFIRMED,
            reason="this review must roll back",
            reviewer="alice@example.com",
        )

    reloaded = ConflictService(session_factory, clock=lambda: T1).get(detected.id)
    assert reloaded == detected
    assert reloaded.status is ConflictStatus.PENDING
    assert ConflictService(session_factory, clock=lambda: T1).history(detected.id) == history_before
