"""U12 conflict rule and service acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from konwledge.application import KnowledgeLibrary
from konwledge.conflicts import (
    ConflictNotFoundError,
    ConflictService,
    ConflictTransitionError,
)
from konwledge.conflicts.rules import (
    PORT_PREDICATE,
    RULE_PORT_MISMATCH,
    conflict_key,
    extract_port_assertion,
    port_mismatch_candidates,
)
from konwledge.demo import build_demo
from konwledge.domain import (
    ConflictEvidence,
    ConflictStatus,
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
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

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
NEXT = NOW + timedelta(hours=1)


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


def evidence(
    value: str,
    *,
    source_id: UUID | None = None,
    document_id: UUID | None = None,
    unit_id: UUID | None = None,
    lineage_id: UUID | None = None,
    subject: str = "order-service",
    predicate: str = PORT_PREDICATE,
    status: KnowledgeUnitStatus = KnowledgeUnitStatus.ACTIVE,
    quote: str | None = None,
) -> ConflictEvidence:
    exact = quote or f"{subject} listens on port {value}"
    return ConflictEvidence(
        source_id=source_id or uuid4(),
        document_id=document_id or uuid4(),
        unit_id=unit_id or uuid4(),
        lineage_id=lineage_id or uuid4(),
        source_uri="notes/service.md",
        locator="service.md#port",
        quote=exact,
        start_char=7,
        end_char=7 + len(exact),
        section_path=("Service", "Port"),
        status=status,
        subject=subject,
        predicate=predicate,
        value=value,
    )


def add_fact(
    uow: UnitOfWork,
    text: str,
    *,
    name: str,
    source_id: UUID | None = None,
    document_id: UUID | None = None,
    unit_id: UUID | None = None,
    lineage_id: UUID | None = None,
    ingested_at: datetime = NOW,
    ordinal: int = 0,
    section_path: tuple[str, ...] = ("Ports",),
) -> tuple[UUID, UUID, UUID, UUID]:
    sid = source_id or uuid4()
    did = document_id or uuid4()
    uid = unit_id or uuid4()
    lineage = lineage_id or uid
    if uow.sources.get(sid) is None:
        uow.sources.add(Source(id=sid, kind=SourceKind.FILE, uri=f"{name}.md", created_at=NOW))
    uow.documents.add(
        Document(
            id=did,
            source_id=sid,
            locator=f"{name}.md",
            content_type=DocumentContentType.MARKDOWN,
            checksum=f"{did.int % 16:x}" * 64,
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
            start_char=11,
            end_char=11 + len(text),
            section_path=section_path,
        )
    )
    return sid, did, uid, lineage


def add_pair(
    session_factory: SessionFactory,
    *,
    left: str = "order-service listens on port 8000",
    right: str = "order-service listens on port 8080",
) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID, UUID, UUID]:
    with UnitOfWork(session_factory) as uow:
        left_ids = add_fact(uow, left, name="left")
        right_ids = add_fact(uow, right, name="right")
        uow.commit()
    return (*left_ids, *right_ids)


def test_rule_requires_same_subject_predicate_different_values_and_sources() -> None:
    source = uuid4()
    baseline = evidence("8000", source_id=source)

    assert port_mismatch_candidates(()) == ()
    assert port_mismatch_candidates((baseline, evidence("8000"))) == ()
    assert port_mismatch_candidates((baseline, evidence("8080", subject="user-service"))) == ()
    assert port_mismatch_candidates((baseline, evidence("30", predicate="timeout"))) == ()
    assert port_mismatch_candidates((baseline, evidence("8080", source_id=source))) == ()

    [candidate] = port_mismatch_candidates((baseline, evidence("8080")))
    assert candidate.rule == RULE_PORT_MISMATCH
    assert candidate.left.predicate == candidate.right.predicate == PORT_PREDICATE
    assert {candidate.left.value, candidate.right.value} == {"8000", "8080"}


@pytest.mark.parametrize(
    "text",
    [
        "service listens on port 8000",
        "order-service and user-service listen on port 8000",
        "order-service does not listen on port 8000",
        "order-service listens on port 8000 or 8080",
        "order-service listens on port 8000-8080",
        "order-service 监听 8000/8080 端口",
        "order-service 监听 8000或9000 端口",
        "order-service 监听端口 123456",
        "order-service config: do not use port=8000",
        "order-service listens on port 0",
        "order-service listens on port 65536",
        "order-service listens on port 8000; port=8080",
    ],
)
def test_rule_rejects_unknown_ambiguous_negated_or_non_scalar_units(text: str) -> None:
    assert extract_port_assertion(text) is None


@pytest.mark.parametrize("port", [1, 65535])
def test_rule_accepts_full_valid_port_boundary(port: int) -> None:
    assertion = extract_port_assertion(f"order-service listens on port {port}")
    assert assertion is not None
    assert (assertion.subject, assertion.value) == ("order-service", str(port))


def test_rule_handles_unicode_whitespace_and_unique_document_context() -> None:
    assertion = extract_port_assertion(" ORDER-service\u3000监听端口： 8080\n")
    assert assertion is not None
    assert (assertion.subject, assertion.value) == ("order-service", "8080")

    fallback = extract_port_assertion(
        "默认监听端口 8000", document_services=frozenset({"order-service"})
    )
    assert fallback is not None and fallback.subject == "order-service"
    assert (
        extract_port_assertion(
            "默认监听端口 8000",
            document_services=frozenset({"order-service", "user-service"}),
        )
        is None
    )


def test_key_is_orientation_independent_and_ignores_version_specific_ids() -> None:
    left_lineage, right_lineage = uuid4(), uuid4()
    left = evidence("8000", lineage_id=left_lineage)
    right = evidence("8080", lineage_id=right_lineage)
    expected = conflict_key(
        rule=RULE_PORT_MISMATCH,
        subject="order-service",
        predicate=PORT_PREDICATE,
        left=left,
        right=right,
    )
    reversed_key = conflict_key(
        rule=RULE_PORT_MISMATCH,
        subject="order-service",
        predicate=PORT_PREDICATE,
        left=right,
        right=left,
    )
    changed_version = left.model_copy(update={"document_id": uuid4(), "unit_id": uuid4()})
    version_key = conflict_key(
        rule=RULE_PORT_MISMATCH,
        subject="order-service",
        predicate=PORT_PREDICATE,
        left=changed_version,
        right=right,
    )
    assert expected == reversed_key == version_key
    assert len(expected) == 64


def test_library_exposes_conflicts_with_injected_clock_and_id_factory(
    session_factory: SessionFactory,
) -> None:
    add_pair(session_factory)
    generated = iter((uuid4(), uuid4()))
    expected_conflict_id = next(generated)
    expected_revision_id = next(generated)
    issued = iter((expected_conflict_id, expected_revision_id))
    library = KnowledgeLibrary(
        session_factory,
        clock=lambda: NOW,
        id_factory=lambda: next(issued),
    )

    [conflict] = library.conflicts.scan(dry_run=False).created

    assert isinstance(library.conflicts, ConflictService)
    assert conflict.id == expected_conflict_id
    assert conflict.detected_at == NOW
    [revision] = library.conflicts.history(conflict.id)
    assert revision.id == expected_revision_id
    assert revision.created_at == NOW


def test_scan_dry_run_consumes_no_ids_and_apply_is_idempotent(
    session_factory: SessionFactory,
) -> None:
    add_pair(session_factory)
    issued: list[UUID] = []

    def next_id() -> UUID:
        result = uuid4()
        issued.append(result)
        return result

    service = ConflictService(session_factory, id_factory=next_id, clock=lambda: NOW)
    preview = service.scan()
    assert preview.dry_run is True
    assert len(preview.candidates) == 1
    assert preview.created == preview.existing == ()
    assert issued == []
    assert service.list() == ()

    applied = service.scan(dry_run=False)
    assert applied.dry_run is False
    assert len(applied.created) == 1
    assert len(issued) == 2  # conflict id plus detection-revision id
    again = service.scan(dry_run=False)
    assert again.created == ()
    assert again.existing == applied.created
    assert len(issued) == 2


def test_apply_rolls_back_conflict_when_revision_id_factory_fails(
    session_factory: SessionFactory,
) -> None:
    add_pair(session_factory)
    calls = 0

    def fail_second_call() -> UUID:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("revision id failure")
        return uuid4()

    service = ConflictService(session_factory, id_factory=fail_second_call, clock=lambda: NOW)
    with pytest.raises(RuntimeError, match="revision id failure"):
        service.scan(dry_run=False)
    assert service.list() == ()

    with UnitOfWork(session_factory) as uow:
        conflict_revisions = [
            revision
            for revision in uow.revisions.list()
            if revision.entity_type is RevisionEntityType.CONFLICT
        ]
    assert conflict_revisions == []


def test_head_active_and_stale_participate_but_retired_and_unpublished_do_not(
    session_factory: SessionFactory,
) -> None:
    with UnitOfWork(session_factory) as uow:
        left_source, left_doc, _, _ = add_fact(
            uow, "order-service listens on port 7000", name="retired", ingested_at=NOW
        )
        head_doc = uuid4()
        add_fact(
            uow,
            "order-service listens on port 8000",
            name="head",
            source_id=left_source,
            document_id=head_doc,
            ingested_at=NEXT,
        )
        add_fact(uow, "order-service listens on port 8080", name="other")
        unpublished_doc = uuid4()
        add_fact(
            uow,
            "order-service listens on port 9000",
            name="unpublished",
            source_id=left_source,
            document_id=unpublished_doc,
            ingested_at=NEXT + timedelta(hours=1),
        )
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.SOURCE,
                entity_id=left_source,
                revision=1,
                change_type=RevisionChangeType.UPDATE,
                created_at=NEXT,
                payload={"change": "published", "document_id": str(head_doc)},
            )
        )
        uow.commit()

    [candidate] = (
        ConflictService(session_factory, clock=lambda: NOW + timedelta(days=1)).scan().candidates
    )
    assert {candidate.left.value, candidate.right.value} == {"8000", "8080"}
    assert left_doc not in {candidate.left.document_id, candidate.right.document_id}
    assert unpublished_doc not in {candidate.left.document_id, candidate.right.document_id}


def test_version_text_change_keeps_key_and_first_evidence_frozen(
    session_factory: SessionFactory,
) -> None:
    left_source, _, old_unit, lineage, _, _, _, _ = add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: NOW)
    [created] = service.scan(dry_run=False).created
    assert old_unit in {created.left.unit_id, created.right.unit_id}

    with UnitOfWork(session_factory) as uow:
        new_unit = uuid4()
        add_fact(
            uow,
            "order-service now listens on port 8000",
            name="left-v2",
            source_id=left_source,
            unit_id=new_unit,
            lineage_id=lineage,
            ingested_at=NEXT,
        )
        uow.commit()

    [candidate] = ConflictService(session_factory, clock=lambda: NEXT).scan().candidates
    [existing] = ConflictService(session_factory, clock=lambda: NEXT).scan().existing
    assert candidate.key == created.key == existing.key
    assert new_unit in {candidate.left.unit_id, candidate.right.unit_id}
    assert old_unit in {existing.left.unit_id, existing.right.unit_id}
    assert new_unit not in {existing.left.unit_id, existing.right.unit_id}


def test_review_states_persist_reopen_and_do_not_change_knowledge_statuses(
    session_factory: SessionFactory,
) -> None:
    *_, left_unit, _, _, _, right_unit, _ = add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: NOW)
    [conflict] = service.scan(dry_run=False).created
    original_evidence = (conflict.left, conflict.right)

    with UnitOfWork(session_factory) as uow:
        before = (uow.knowledge_units.get(left_unit), uow.knowledge_units.get(right_unit))
    assert before[0] is not None and before[1] is not None

    confirmed = service.review(
        conflict.id,
        status=ConflictStatus.CONFIRMED,
        reason="Sources genuinely disagree",
        reviewer="reviewer@example.com",
    )
    assert confirmed.status is ConflictStatus.CONFIRMED
    assert confirmed.reviewed_at == NOW
    assert confirmed.review_reason == "Sources genuinely disagree"
    assert confirmed.reviewer == "reviewer@example.com"
    assert (confirmed.left, confirmed.right) == original_evidence
    assert service.list(status=ConflictStatus.CONFIRMED) == (confirmed,)

    reopened = service.review(
        conflict.id,
        status=ConflictStatus.PENDING,
        reason="New investigation requested",
    )
    assert reopened.status is ConflictStatus.PENDING
    assert reopened.review_reason == "New investigation requested"
    assert (reopened.left, reopened.right) == original_evidence

    reloaded = ConflictService(session_factory, clock=lambda: NEXT).get(conflict.id)
    assert reloaded == reopened
    history = service.history(conflict.id)
    assert [revision.revision for revision in history] == [1, 2, 3]
    assert [revision.payload["change"] for revision in history] == [
        "detected",
        "reviewed",
        "reviewed",
    ]

    with UnitOfWork(session_factory) as uow:
        after = (uow.knowledge_units.get(left_unit), uow.knowledge_units.get(right_unit))
    assert after == before


@pytest.mark.parametrize("reason", ["", "   "])
def test_review_rejects_blank_reason(session_factory: SessionFactory, reason: str) -> None:
    add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: NOW)
    [conflict] = service.scan(dry_run=False).created
    with pytest.raises(ConflictTransitionError, match="non-empty reason"):
        service.review(conflict.id, status=ConflictStatus.DISMISSED, reason=reason)


def test_review_rejects_same_status_and_missing_conflicts(
    session_factory: SessionFactory,
) -> None:
    add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: NOW)
    [conflict] = service.scan(dry_run=False).created
    with pytest.raises(ConflictTransitionError, match="already pending"):
        service.review(conflict.id, status=ConflictStatus.PENDING, reason="duplicate")

    missing = uuid4()
    with pytest.raises(ConflictNotFoundError):
        service.get(missing)
    with pytest.raises(ConflictNotFoundError):
        service.history(missing)
    with pytest.raises(ConflictNotFoundError):
        service.review(missing, status=ConflictStatus.RESOLVED, reason="missing")


def test_review_rejects_clock_earlier_than_detection_or_latest_review_without_mutation(
    session_factory: SessionFactory,
) -> None:
    add_pair(session_factory)
    detected_service = ConflictService(session_factory, clock=lambda: NOW)
    [conflict] = detected_service.scan(dry_run=False).created
    detected_history = detected_service.history(conflict.id)

    before_detection = ConflictService(
        session_factory,
        clock=lambda: NOW - timedelta(seconds=1),
    )
    with pytest.raises(ConflictTransitionError, match="earlier"):
        before_detection.review(
            conflict.id,
            status=ConflictStatus.CONFIRMED,
            reason="clock precedes detection",
        )
    assert before_detection.get(conflict.id) == conflict
    assert before_detection.history(conflict.id) == detected_history

    review_service = ConflictService(session_factory, clock=lambda: NEXT)
    reviewed = review_service.review(
        conflict.id,
        status=ConflictStatus.CONFIRMED,
        reason="first review",
    )
    reviewed_history = review_service.history(conflict.id)

    before_latest_review = ConflictService(
        session_factory,
        clock=lambda: NOW + timedelta(minutes=30),
    )
    with pytest.raises(ConflictTransitionError, match="earlier"):
        before_latest_review.review(
            conflict.id,
            status=ConflictStatus.RESOLVED,
            reason="clock precedes latest review",
        )
    assert before_latest_review.get(conflict.id) == reviewed
    assert before_latest_review.history(conflict.id) == reviewed_history


def test_historical_scan_is_preview_only_and_apply_rejects_future_revisions(
    session_factory: SessionFactory,
) -> None:
    left_source, _, _, _, _, _, _, _ = add_pair(session_factory)
    service = ConflictService(session_factory, clock=lambda: NOW)
    historical = service.scan(at=NOW)
    assert historical.dry_run is True and len(historical.candidates) == 1
    with pytest.raises(ValueError, match="historical conflict scans must be dry runs"):
        service.scan(at=NOW, dry_run=False)

    with UnitOfWork(session_factory) as uow:
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.SOURCE,
                entity_id=left_source,
                revision=1,
                change_type=RevisionChangeType.UPDATE,
                created_at=NEXT,
                payload={"change": "future update"},
            )
        )
        uow.commit()
    with pytest.raises(ValueError, match="changed after scan time"):
        service.scan(dry_run=False)


def test_demo_scan_has_exact_expected_conflict_and_exact_evidence(tmp_path: Path) -> None:
    demo = build_demo(tmp_path / "demo")
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{demo.database.as_posix()}")
    try:
        sessions = create_session_factory(engine)
        [candidate] = ConflictService(sessions, clock=lambda: NOW).scan().candidates
        assert candidate.rule == RULE_PORT_MISMATCH
        assert candidate.left.subject == candidate.right.subject == "order-service"
        assert {candidate.left.value, candidate.right.value} == {"8000", "8080"}
        assert {candidate.left.status, candidate.right.status} == {
            KnowledgeUnitStatus.ACTIVE,
            KnowledgeUnitStatus.STALE,
        }

        with UnitOfWork(sessions) as uow:
            for item in (candidate.left, candidate.right):
                source = uow.sources.get(item.source_id)
                document = uow.documents.get(item.document_id)
                unit = uow.knowledge_units.get(item.unit_id)
                assert source is not None and source.uri == item.source_uri
                assert document is not None and document.locator == item.locator
                assert unit is not None
                assert unit.lineage_id == item.lineage_id
                assert unit.text == item.quote
                assert (unit.start_char, unit.end_char) == (item.start_char, item.end_char)
                assert unit.section_path == item.section_path
                assert len(item.quote) == item.end_char - item.start_char
    finally:
        engine.dispose()
