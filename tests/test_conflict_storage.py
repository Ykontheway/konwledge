from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import IntegrityError

from konwledge.domain import (
    Conflict,
    ConflictEvidence,
    ConflictStatus,
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.storage import (
    EntityNotFoundError,
    ImmutableEntityError,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.tables import Base, ConflictRow

SOURCE_A = UUID("10000000-0000-0000-0000-000000000001")
SOURCE_B = UUID("10000000-0000-0000-0000-000000000002")
DOC_A = UUID("20000000-0000-0000-0000-000000000001")
DOC_B = UUID("20000000-0000-0000-0000-000000000002")
UNIT_A = UUID("30000000-0000-0000-0000-000000000001")
UNIT_B = UUID("30000000-0000-0000-0000-000000000002")
LINEAGE_A = UUID("40000000-0000-0000-0000-000000000001")
LINEAGE_B = UUID("40000000-0000-0000-0000-000000000002")
CONFLICT_ID = UUID("50000000-0000-0000-0000-000000000001")

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


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


def make_evidence(
    source_id: UUID, doc_id: UUID, unit_id: UUID, lineage_id: UUID, value: str
) -> ConflictEvidence:
    return ConflictEvidence(
        source_id=source_id,
        document_id=doc_id,
        unit_id=unit_id,
        lineage_id=lineage_id,
        source_uri=f"file://{source_id}.md",
        locator=f"{doc_id}.md",
        quote=f"Port is {value}",
        start_char=0,
        end_char=8 + len(value),
        status=KnowledgeUnitStatus.ACTIVE,
        subject="test-service",
        predicate="port",
        value=value,
    )


def make_conflict(
    conflict_id: UUID = CONFLICT_ID,
    key: str | None = None,
    status: ConflictStatus = ConflictStatus.PENDING,
) -> Conflict:
    left = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")
    right = make_evidence(SOURCE_B, DOC_B, UNIT_B, LINEAGE_B, "8080")
    return Conflict(
        id=conflict_id,
        key=key or ("a" * 64),
        rule="port_mismatch",
        reason="port mismatch detected",
        left=left,
        right=right,
        status=status,
        detected_at=NOW,
    )


def setup_metadata(uow: UnitOfWork) -> None:
    for sid, did, uid, lid in [
        (SOURCE_A, DOC_A, UNIT_A, LINEAGE_A),
        (SOURCE_B, DOC_B, UNIT_B, LINEAGE_B),
    ]:
        uow.sources.add(
            Source(id=sid, kind=SourceKind.FILE, uri=f"file://{sid}.md", created_at=NOW)
        )
        uow.documents.add(
            Document(
                id=did,
                source_id=sid,
                locator=f"{did}.md",
                content_type=DocumentContentType.TEXT,
                checksum="a" * 64,
                ingested_at=NOW,
            )
        )
        uow.knowledge_units.add(
            KnowledgeUnit(
                id=uid,
                lineage_id=lid,
                document_id=did,
                ordinal=0,
                text="Port is 8000" if uid == UNIT_A else "Port is 8080",
                start_char=0,
                end_char=12,
                status=KnowledgeUnitStatus.ACTIVE,
            )
        )


def test_conflict_repository_roundtrip(session_factory: SessionFactory) -> None:
    conflict = make_conflict()
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        uow.conflicts.add(conflict)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        restored = uow.conflicts.get(CONFLICT_ID)
        assert restored == conflict
        assert uow.conflicts.get_by_key(conflict.key) == conflict
        assert list(uow.conflicts.list()) == [conflict]


def test_conflict_repository_unique_key(session_factory: SessionFactory) -> None:
    c1 = make_conflict(conflict_id=uuid4(), key="a" * 64)
    c2 = make_conflict(conflict_id=uuid4(), key="a" * 64)
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        uow.conflicts.add(c1)
        with pytest.raises(IntegrityError):
            uow.conflicts.add(c2)
        uow.rollback()


def test_conflict_repository_rejects_source_document_fk_mismatch(
    session_factory: SessionFactory,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        conflict = make_conflict()
        mismatched = Conflict.model_validate(
            {
                **conflict.model_dump(),
                "right": {
                    **conflict.right.model_dump(),
                    "document_id": DOC_A,
                },
            }
        )
        with pytest.raises(IntegrityError):
            uow.conflicts.add(mismatched)
        uow.rollback()


def test_conflict_repository_rejects_document_unit_fk_mismatch(
    session_factory: SessionFactory,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        conflict = make_conflict()
        mismatched = Conflict.model_validate(
            {
                **conflict.model_dump(),
                "right": {
                    **conflict.right.model_dump(),
                    "unit_id": UNIT_A,
                },
            }
        )
        with pytest.raises(IntegrityError):
            uow.conflicts.add(mismatched)
        uow.rollback()


def test_conflict_repository_immutability(session_factory: SessionFactory) -> None:
    conflict = make_conflict()
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        uow.conflicts.add(conflict)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        updated = conflict.model_copy(
            update={
                "status": ConflictStatus.CONFIRMED,
                "reviewed_at": NOW,
                "review_reason": "Correct",
            }
        )
        uow.conflicts.update(updated)

        bad = conflict.model_copy(update={"reason": "New reason"})
        with pytest.raises(ImmutableEntityError, match="reason is immutable"):
            uow.conflicts.update(bad)

        with pytest.raises(ImmutableEntityError, match="immutable"):
            uow.conflicts.delete(CONFLICT_ID)


def test_conflict_domain_deep_json_roundtrip() -> None:
    conflict = make_conflict()
    restored = Conflict.model_validate_json(conflict.model_dump_json())
    assert restored == conflict
    assert restored.left == conflict.left
    assert restored.left.section_path == conflict.left.section_path
    assert restored.left.status is KnowledgeUnitStatus.ACTIVE


def test_conflict_domain_validation() -> None:
    evidence = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")

    # end_char <= start_char
    with pytest.raises(ValidationError, match="end_char must be greater than start_char"):
        ConflictEvidence.model_validate({**evidence.model_dump(), "start_char": 10, "end_char": 10})

    # quote length mismatch
    with pytest.raises(ValidationError, match="quote length must match"):
        ConflictEvidence.model_validate({**evidence.model_dump(), "quote": "wrong"})

    # Same source conflict
    with pytest.raises(ValidationError, match="different sources"):
        Conflict(
            id=uuid4(),
            key="a" * 64,
            rule="rule",
            reason="reason",
            left=make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000"),
            right=make_evidence(SOURCE_A, DOC_A, UNIT_B, LINEAGE_B, "8080"),
            detected_at=NOW,
        )

    # Missing review info for non-pending
    with pytest.raises(ValidationError, match="require reviewed_at"):
        Conflict.model_validate(
            {**make_conflict().model_dump(), "status": ConflictStatus.CONFIRMED}
        )


def test_create_schema_upgrades_pre_u12_database_without_data_loss() -> None:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    try:
        old_tables = [
            table for table in Base.metadata.sorted_tables if table is not ConflictRow.__table__
        ]
        Base.metadata.create_all(database, tables=old_tables)
        assert "conflicts" not in inspect(database).get_table_names()
        sessions = create_session_factory(database)
        with UnitOfWork(sessions) as uow:
            setup_metadata(uow)
            legacy_sources = list(uow.sources.list())
            legacy_documents = list(uow.documents.list())
            legacy_units = list(uow.knowledge_units.list())
            uow.commit()

        create_schema(database)
        create_schema(database)

        assert "conflicts" in inspect(database).get_table_names()
        conflict = make_conflict()
        with UnitOfWork(sessions) as uow:
            assert list(uow.conflicts.list()) == []
            # The newly created table can reference metadata predating the upgrade.
            uow.conflicts.add(conflict)
            uow.commit()

        create_schema(database)
        create_schema(database)
        with UnitOfWork(sessions) as uow:
            assert list(uow.sources.list()) == legacy_sources
            assert list(uow.documents.list()) == legacy_documents
            assert list(uow.knowledge_units.list()) == legacy_units
            assert uow.conflicts.get(conflict.id) == conflict
            assert uow.conflicts.get_by_key(conflict.key) == conflict
            assert list(uow.conflicts.list()) == [conflict]
    finally:
        database.dispose()


@pytest.fixture
def stored_conflict(session_factory: SessionFactory) -> Conflict:
    conflict = make_conflict()
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        uow.conflicts.add(conflict)
        uow.commit()
    return conflict


@pytest.mark.parametrize(
    "field", ["source_uri", "locator", "subject", "predicate", "value", "quote"]
)
@pytest.mark.parametrize("blank", ["", " \t\n "], ids=["empty", "whitespace"])
def test_conflict_evidence_rejects_blank_fields(field: str, blank: str) -> None:
    evidence = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")
    payload = {**evidence.model_dump(), field: blank}
    if field == "quote":
        # Keep nonempty whitespace quotes range-valid to isolate blank validation.
        payload["end_char"] = max(1, len(blank))
    with pytest.raises(ValidationError) as error:
        ConflictEvidence.model_validate(payload)
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("blank", ["", " ", "\t\n"], ids=["empty", "space", "tabs-newline"])
def test_conflict_evidence_rejects_blank_section_path(blank: str) -> None:
    evidence = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")
    with pytest.raises(ValidationError, match="section_path entries must not be empty"):
        ConflictEvidence.model_validate(
            {**evidence.model_dump(), "section_path": ("Overview", blank, "Ports")}
        )


@pytest.mark.parametrize("status", [KnowledgeUnitStatus.SUPERSEDED, KnowledgeUnitStatus.DELETED])
def test_conflict_evidence_rejects_retired_status(status: KnowledgeUnitStatus) -> None:
    evidence = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")
    with pytest.raises(ValidationError, match="status must be active or stale"):
        ConflictEvidence.model_validate({**evidence.model_dump(), "status": status})


@pytest.mark.parametrize("status", [KnowledgeUnitStatus.ACTIVE, KnowledgeUnitStatus.STALE])
def test_conflict_evidence_normalizes_metadata_but_preserves_quote(
    status: KnowledgeUnitStatus,
) -> None:
    evidence = make_evidence(SOURCE_A, DOC_A, UNIT_A, LINEAGE_A, "8000")
    quote = " \tPort is 8000\n "
    normalized = ConflictEvidence.model_validate(
        {
            **evidence.model_dump(),
            "source_uri": f"  {evidence.source_uri}\t",
            "locator": f"  {evidence.locator}\t",
            "subject": " test-service ",
            "predicate": " port ",
            "value": " 8000 ",
            "section_path": (" Overview ", "\tPorts\n"),
            "quote": quote,
            "end_char": len(quote),
            "status": status,
        }
    )
    assert normalized.source_uri == evidence.source_uri
    assert normalized.locator == evidence.locator
    assert normalized.subject == evidence.subject
    assert normalized.predicate == evidence.predicate
    assert normalized.value == evidence.value
    assert normalized.section_path == ("Overview", "Ports")
    assert normalized.quote == quote
    assert normalized.status is status
    assert ConflictEvidence.model_validate_json(normalized.model_dump_json()) == normalized


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("source_id", "different sources"),
        ("subject", "same subject"),
        ("predicate", "same predicate"),
        ("value", "disagree on value"),
    ],
)
def test_conflict_rejects_inconsistent_evidence_relationships(field: str, message: str) -> None:
    conflict = make_conflict()
    # Both evidence objects remain independently valid; only their relationship changes.
    right = ConflictEvidence.model_validate(
        {
            **conflict.right.model_dump(),
            field: f"different {field}"
            if field in {"subject", "predicate"}
            else getattr(conflict.left, field),
        }
    )
    with pytest.raises(ValidationError, match=message):
        Conflict.model_validate({**conflict.model_dump(), "right": right})


@pytest.mark.parametrize(
    "reviewed_at",
    [NOW - timedelta(seconds=1), datetime(2026, 11, 9, 16, tzinfo=timezone(timedelta(hours=8)))],
    ids=["same-timezone", "different-timezone"],
)
def test_conflict_rejects_review_before_detection(reviewed_at: datetime) -> None:
    with pytest.raises(ValidationError, match="reviewed_at must be greater than or equal"):
        Conflict.model_validate(
            {
                **make_conflict().model_dump(),
                "status": ConflictStatus.CONFIRMED,
                "reviewed_at": reviewed_at,
                "review_reason": "Verified",
            }
        )


@pytest.mark.parametrize(
    "status", [ConflictStatus.CONFIRMED, ConflictStatus.DISMISSED, ConflictStatus.RESOLVED]
)
def test_conflict_non_pending_requires_review_reason(status: ConflictStatus) -> None:
    with pytest.raises(ValidationError, match="non-pending conflicts require review_reason"):
        Conflict.model_validate(
            {**make_conflict().model_dump(), "status": status, "reviewed_at": NOW}
        )


@pytest.mark.parametrize(
    "status", [ConflictStatus.CONFIRMED, ConflictStatus.DISMISSED, ConflictStatus.RESOLVED]
)
def test_conflict_non_pending_requires_reviewed_at(status: ConflictStatus) -> None:
    with pytest.raises(ValidationError, match="non-pending conflicts require reviewed_at"):
        Conflict.model_validate(
            {**make_conflict().model_dump(), "status": status, "review_reason": "Verified"}
        )


@pytest.mark.parametrize("field", ["rule", "reason", "reviewer", "review_reason"])
@pytest.mark.parametrize("blank", ["", " \t\n "], ids=["empty", "whitespace"])
def test_conflict_rejects_blank_review_and_detection_metadata(field: str, blank: str) -> None:
    with pytest.raises(ValidationError) as error:
        Conflict.model_validate(
            {
                **make_conflict().model_dump(),
                "status": ConflictStatus.CONFIRMED,
                "reviewed_at": NOW,
                "reviewer": "Reviewer",
                "review_reason": "Verified",
                field: blank,
            }
        )
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("field", ["detected_at", "reviewed_at"])
def test_conflict_rejects_naive_datetime(field: str) -> None:
    with pytest.raises(ValidationError, match=f"{field} must include timezone information"):
        Conflict.model_validate(
            {
                **make_conflict().model_dump(),
                "status": ConflictStatus.CONFIRMED,
                "reviewed_at": NOW,
                "review_reason": "Verified",
                field: NOW.replace(tzinfo=None),
            }
        )


@pytest.mark.parametrize("has_existing_conflict", [False, True], ids=["empty", "populated"])
def test_conflict_repository_update_missing_id(
    session_factory: SessionFactory, has_existing_conflict: bool
) -> None:
    existing = make_conflict()
    missing = Conflict.model_validate(
        {
            **existing.model_dump(),
            "id": uuid4(),
            "key": "b" * 64,
            "reason": "A distinct detection not yet persisted",
        }
    )
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        if has_existing_conflict:
            uow.conflicts.add(existing)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        with pytest.raises(EntityNotFoundError, match=f"ConflictRow {missing.id} was not found"):
            uow.conflicts.update(missing)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.conflicts.get(missing.id) is None
        assert uow.conflicts.get_by_key(missing.key) is None
        assert list(uow.conflicts.list()) == ([existing] if has_existing_conflict else [])


@pytest.mark.parametrize("change_key", [False, True], ids=["same-key", "same-signature"])
def test_conflict_repository_rejects_changed_identity(
    session_factory: SessionFactory, stored_conflict: Conflict, change_key: bool
) -> None:
    renamed = Conflict.model_validate(
        {
            **stored_conflict.model_dump(),
            "id": uuid4(),
            "key": "b" * 64 if change_key else stored_conflict.key,
            "status": ConflictStatus.CONFIRMED,
            "reviewed_at": NOW,
            "review_reason": "Review does not permit changing identity",
        }
    )
    with UnitOfWork(session_factory) as uow:
        with pytest.raises(ImmutableEntityError, match="conflict identity is immutable"):
            uow.conflicts.update(renamed)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.conflicts.get(renamed.id) is None
        assert uow.conflicts.get_by_key(stored_conflict.key) == stored_conflict
        if change_key:
            assert uow.conflicts.get_by_key(renamed.key) is None
        assert list(uow.conflicts.list()) == [stored_conflict]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("key", "conflict key is immutable"),
        ("rule", "conflict rule is immutable"),
        ("reason", "conflict reason is immutable"),
        ("left", "conflict evidence is immutable"),
        ("right", "conflict evidence is immutable"),
        ("detected_at", "conflict detected_at is immutable"),
    ],
)
def test_conflict_repository_rejects_immutable_field_changes(
    session_factory: SessionFactory, stored_conflict: Conflict, field: str, message: str
) -> None:
    payload = stored_conflict.model_dump()
    if field in {"left", "right"}:
        evidence = getattr(stored_conflict, field)
        payload[field] = {**evidence.model_dump(), "section_path": ("Changed snapshot",)}
    elif field == "detected_at":
        payload[field] = NOW + timedelta(seconds=1)
    else:
        payload[field] = "b" * 64 if field == "key" else "Changed detection metadata"
    # Also change mutable review state, so rejection must leave the entire row untouched.
    payload.update(
        status=ConflictStatus.CONFIRMED,
        reviewed_at=NOW + timedelta(minutes=1),
        review_reason="Verified",
        reviewer="Reviewer",
    )
    changed = Conflict.model_validate(payload)
    with UnitOfWork(session_factory) as uow:
        with pytest.raises(ImmutableEntityError, match=message):
            uow.conflicts.update(changed)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.conflicts.get(stored_conflict.id) == stored_conflict
        assert uow.conflicts.get_by_key(stored_conflict.key) == stored_conflict
        if field == "key":
            assert uow.conflicts.get_by_key(changed.key) is None
        assert list(uow.conflicts.list()) == [stored_conflict]


@pytest.mark.parametrize(
    "status", [ConflictStatus.CONFIRMED, ConflictStatus.DISMISSED, ConflictStatus.RESOLVED]
)
def test_conflict_repository_persists_review_without_changing_evidence(
    session_factory: SessionFactory, stored_conflict: Conflict, status: ConflictStatus
) -> None:
    reviewed = Conflict.model_validate(
        {
            **stored_conflict.model_dump(),
            "status": status,
            # Equal instants in different zones are allowed at the review boundary.
            "reviewed_at": NOW.astimezone(timezone(timedelta(hours=8))),
            "review_reason": " Verified against both sources ",
            "reviewer": " Reviewer ",
        }
    )
    with UnitOfWork(session_factory) as uow:
        uow.conflicts.update(reviewed)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        restored = uow.conflicts.get(stored_conflict.id)
        assert restored == reviewed
        assert restored is not None
        assert restored.left == stored_conflict.left
        assert restored.right == stored_conflict.right
        assert restored.status is status
        assert restored.review_reason == "Verified against both sources"
        assert restored.reviewer == "Reviewer"
        assert uow.conflicts.get_by_key(stored_conflict.key) == reviewed
        assert list(uow.conflicts.list()) == [reviewed]


@pytest.mark.parametrize("key", ["a" * 64, "b" * 64, "c" * 64], ids=["first", "second", "missing"])
def test_conflict_repository_get_by_key(
    session_factory: SessionFactory, stored_conflict: Conflict, key: str
) -> None:
    second = make_conflict(conflict_id=uuid4(), key="b" * 64)
    with UnitOfWork(session_factory) as uow:
        uow.conflicts.add(second)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        expected = {stored_conflict.key: stored_conflict, second.key: second}.get(key)
        assert uow.conflicts.get_by_key(key) == expected


def test_conflict_repository_sorts_by_instant_then_id(session_factory: SessionFactory) -> None:
    # ISO strings sort 06:00-05:00 first and 18:00+08:00 last, unlike actual instants.
    detections = [
        (4, NOW),
        (1, (NOW + timedelta(hours=1)).astimezone(timezone(timedelta(hours=8)))),
        (2, NOW + timedelta(hours=1)),
        (3, (NOW + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-5)))),
    ]
    expected = [
        Conflict.model_validate(
            {
                **make_conflict().model_dump(),
                "id": UUID(int=number),
                "key": f"{number:064x}",
                "detected_at": detected_at,
            }
        )
        for number, detected_at in detections
    ]
    with UnitOfWork(session_factory) as uow:
        setup_metadata(uow)
        for conflict in [expected[1], expected[3], expected[2], expected[0]]:
            uow.conflicts.add(conflict)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert list(uow.conflicts.list()) == expected
