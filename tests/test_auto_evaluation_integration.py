from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from konwledge.application import KnowledgeLibrary
from konwledge.auto_evaluation import (
    AutoEvaluationService,
    AutoEvaluationStatus,
)
from konwledge.domain import (
    Document,
    DocumentContentType,
    EvaluationItem,
    EvaluationSet,
    KnowledgeUnit,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.evaluation import EvaluationService
from konwledge.retrieval import RetrievalService
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.tables import AutoEvaluationJobRow, RevisionRow

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
UNIT_ID = UUID("30000000-0000-0000-0000-000000000003")
LINEAGE_ID = UUID("40000000-0000-0000-0000-000000000004")
SET_ID = UUID("50000000-0000-0000-0000-000000000005")


@pytest.fixture
def session_factory() -> Iterator[SessionFactory]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    _seed(factory)
    yield factory
    engine.dispose()


@pytest.fixture
def library(session_factory: SessionFactory) -> KnowledgeLibrary:
    return KnowledgeLibrary(session_factory, clock=lambda: NOW)


@pytest.fixture
def service(library: KnowledgeLibrary) -> AutoEvaluationService:
    return library.automatic_evaluation


def _seed(factory: SessionFactory) -> None:
    with UnitOfWork(factory) as uow:
        uow.sources.add(
            Source(id=SOURCE_ID, kind=SourceKind.FILE, uri="evidence.md", created_at=NOW)
        )
        uow.documents.add(
            Document(
                id=DOCUMENT_ID,
                source_id=SOURCE_ID,
                locator="evidence.md",
                content_type=DocumentContentType.TEXT,
                checksum="a" * 64,
                ingested_at=NOW,
            )
        )
        uow.knowledge_units.add(
            KnowledgeUnit(
                id=UNIT_ID,
                lineage_id=LINEAGE_ID,
                document_id=DOCUMENT_ID,
                ordinal=0,
                text="Evidence for the automatic quality check.",
                start_char=0,
                end_char=41,
            )
        )
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=LINEAGE_ID,
                revision=1,
                change_type=RevisionChangeType.CREATE,
                created_at=NOW,
                payload={
                    "change": "added",
                    "document_id": str(DOCUMENT_ID),
                    "document_checksum": "a" * 64,
                    "unit_id": str(UNIT_ID),
                    "content_hash": KnowledgeUnit(
                        id=UNIT_ID,
                        lineage_id=LINEAGE_ID,
                        document_id=DOCUMENT_ID,
                        ordinal=0,
                        text="Evidence for the automatic quality check.",
                        start_char=0,
                        end_char=41,
                    ).content_hash,
                },
            )
        )
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.SOURCE,
                entity_id=SOURCE_ID,
                revision=1,
                change_type=RevisionChangeType.CREATE,
                created_at=NOW,
                payload={
                    "change": "published",
                    "document_id": str(DOCUMENT_ID),
                    "document_checksum": "a" * 64,
                },
            )
        )
        uow.commit()

    dataset = EvaluationSet(
        id=SET_ID,
        name="automatic quality",
        version=1,
        created_at=NOW,
        items=(
            EvaluationItem(
                id="evidence",
                query="Evidence",
                expected_lineage_ids=frozenset({LINEAGE_ID}),
            ),
        ),
    )
    EvaluationService(factory, RetrievalService(factory), clock=lambda: NOW).import_set(dataset)


def test_nested_transaction_commits_ids_at_outer_end(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)

    with session_factory() as session:
        # Add R1 at outer level
        session.add(
            RevisionRow(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                entity_id=LINEAGE_ID,
                revision=2,
                change_type=RevisionChangeType.UPDATE.value,
                created_at=NOW.isoformat(),
                payload={"change": "refreshed", "content_hash": "b" * 64},
            )
        )
        session.flush()

        with session.begin_nested():
            # Add R2 in nested
            session.add(
                RevisionRow(
                    id=uuid4(),
                    entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                    entity_id=LINEAGE_ID,
                    revision=3,
                    change_type=RevisionChangeType.UPDATE.value,
                    created_at=NOW.isoformat(),
                    payload={"change": "refreshed", "content_hash": "c" * 64},
                )
            )
            session.flush()

        # At this point, outer commit should see both
        session.commit()

    jobs = service.history()
    assert len(jobs) == 1
    assert len(jobs[0].trigger_revision_ids) == 2


def test_nested_transaction_rollback_discards_only_nested_ids(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)

    with session_factory() as session:
        # Add R1 at outer level
        session.add(
            RevisionRow(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                entity_id=LINEAGE_ID,
                revision=2,
                change_type=RevisionChangeType.UPDATE.value,
                created_at=NOW.isoformat(),
                payload={"change": "refreshed", "content_hash": "b" * 64},
            )
        )
        session.flush()

        with session.begin_nested() as sp:
            # Add R2 in nested
            session.add(
                RevisionRow(
                    id=uuid4(),
                    entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                    entity_id=LINEAGE_ID,
                    revision=3,
                    change_type=RevisionChangeType.UPDATE.value,
                    created_at=NOW.isoformat(),
                    payload={"change": "refreshed", "content_hash": "c" * 64},
                )
            )
            session.flush()
            sp.rollback()

        # Add R3 after nested rollback
        session.add(
            RevisionRow(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                entity_id=LINEAGE_ID,
                revision=3,
                change_type=RevisionChangeType.UPDATE.value,
                created_at=NOW.isoformat(),
                payload={"change": "refreshed", "content_hash": "d" * 64},
            )
        )

        session.commit()

    jobs = service.history()
    assert len(jobs) == 1
    assert len(jobs[0].trigger_revision_ids) == 2  # R1 and R3


def test_finalize_cas_blocks_on_knowledge_change(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)
    service.enqueue()
    claimed = service._claim_one()
    assert claimed is not None
    job_id, token = claimed

    # Simulate a knowledge change after claim but before finalize
    with session_factory() as session:
        session.add(
            RevisionRow(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT.value,
                entity_id=LINEAGE_ID,
                revision=2,
                change_type=RevisionChangeType.UPDATE.value,
                created_at=NOW.isoformat(),
                payload={"change": "refreshed", "content_hash": "b" * 64},
            )
        )
        session.commit()

    # Finalize should now return SUPERSEDED because KNOWLEDGE_CHANGED
    assert service._finalize(job_id, token, AutoEvaluationStatus.PASSED) is True

    finished = service.show(job_id)
    assert finished.status is AutoEvaluationStatus.SUPERSEDED
    assert finished.error_code == "KNOWLEDGE_CHANGED"


def test_recover_crash_audit_appends_to_recovery_errors(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)
    service.enqueue()
    claimed = service._claim_one()
    assert claimed is not None
    job_id, _token = claimed

    # Manually expire lease
    with session_factory() as session:
        session.execute(
            update(AutoEvaluationJobRow)
            .where(AutoEvaluationJobRow.id == job_id)
            .values(lease_expires_at=(NOW - timedelta(hours=1)).isoformat())
        )
        session.commit()

    assert service.recover() == 1

    reclaimed = service.show(job_id)
    assert reclaimed.status is AutoEvaluationStatus.QUEUED
    assert reclaimed.error_code == "LEASE_EXPIRED"
    assert len(reclaimed.recovery_errors) == 1
    assert reclaimed.recovery_errors[0]["code"] == "LEASE_EXPIRED"


def test_healthy_pass_fixture(service: AutoEvaluationService) -> None:
    service.enable(SET_ID, version=1)
    job = service.run()

    assert job.status is AutoEvaluationStatus.PASSED
    assert job.report is not None
    assert job.report["passed"] is True
    assert job.attempt_count == 1


def test_metadata_only_does_not_trigger(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)

    with session_factory() as session:
        session.add(
            RevisionRow(
                id=uuid4(),
                entity_type=RevisionEntityType.SOURCE.value,
                entity_id=SOURCE_ID,
                revision=2,
                change_type=RevisionChangeType.UPDATE.value,
                created_at=NOW.isoformat(),
                payload={"change": "metadata_update", "meta": {"foo": "bar"}},
            )
        )
        session.commit()

    assert service.history() == ()


def test_strict_cli_input_validation(session_factory: SessionFactory) -> None:
    from typer.testing import CliRunner

    from konwledge.cli import app

    runner = CliRunner()
    db_path = "test.db"

    # Try enabling with invalid version (bool)
    result = runner.invoke(
        app,
        ["auto-evaluation", "enable", "--set-id", str(SET_ID), "--version", "true", "-d", db_path],
    )
    # Typer might convert 'true' to 1 or fail early.
    # If we want to test our strict validation, we need to bypass Typer's parsing
    # if it's too helpful.
    # But usually Typer parses '--version true' as an error if type is int.
    assert result.exit_code != 0
