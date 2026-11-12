from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from konwledge.auto_evaluation import (
    AutoEvaluationError,
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
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.tables import AutoEvaluationJobRow

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
def service(session_factory: SessionFactory) -> AutoEvaluationService:
    return AutoEvaluationService(session_factory, clock=lambda: NOW)


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
                query="absent-token-zzzz",
                expected_lineage_ids=frozenset({LINEAGE_ID}),
            ),
        ),
    )
    EvaluationService(factory, RetrievalService(factory), clock=lambda: NOW).import_set(dataset)


def _change(factory: SessionFactory, *, revision: int = 2, commit: bool = True) -> UUID:
    revision_id = uuid4()
    with UnitOfWork(factory) as uow:
        uow.revisions.add(
            Revision(
                id=revision_id,
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=LINEAGE_ID,
                revision=revision,
                change_type=RevisionChangeType.UPDATE,
                created_at=NOW,
                payload={
                    "change": "refreshed",
                    "document_id": str(DOCUMENT_ID),
                    "unit_id": str(UNIT_ID),
                    "content_hash": "b" * 64,
                },
            )
        )
        if commit:
            uow.commit()
    return revision_id


def test_disabled_default_and_rollback_leave_no_jobs(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    assert service.status() is None
    _change(session_factory, commit=False)
    assert service.history() == ()

    _change(session_factory)
    assert service.history() == ()


def test_enable_pins_dataset_without_promoting_existing_head(
    service: AutoEvaluationService,
) -> None:
    config = service.enable(SET_ID, version=1)

    assert config.enabled is True
    assert config.dataset_id == SET_ID
    assert config.dataset_version == 1
    assert len(config.dataset_hash) == 64
    assert service.history() == ()


def test_enable_rejects_missing_dataset_and_vector_without_provider(
    service: AutoEvaluationService,
) -> None:
    with pytest.raises(LookupError, match="was not found"):
        service.enable(uuid4(), version=1)
    with pytest.raises(AutoEvaluationError, match="persistent configured provider"):
        service.enable(SET_ID, version=1, mode=RetrievalMode.VECTOR)


def test_committed_change_creates_and_synchronously_finishes_job(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)
    revision_id = _change(session_factory)

    jobs = service.history()
    assert len(jobs) == 1
    assert jobs[0].trigger_revision_ids == (revision_id,)
    assert jobs[0].status is AutoEvaluationStatus.FAILED
    assert jobs[0].error_code == "QUALITY_GATE_FAILED"
    assert jobs[0].finished_at is not None
    assert jobs[0].report is not None
    assert "query" not in str(jobs[0].report).lower()
    assert "evidence.md" not in str(jobs[0].report)


def test_non_knowledge_revision_does_not_enqueue(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)
    with UnitOfWork(session_factory) as uow:
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.SOURCE,
                entity_id=SOURCE_ID,
                revision=2,
                change_type=RevisionChangeType.UPDATE,
                created_at=NOW,
                payload={"change": "metadata_only", "metadata": {}},
            )
        )
        uow.commit()

    assert service.history() == ()


def test_read_only_quality_and_manual_enqueue_do_not_recurse(
    service: AutoEvaluationService,
) -> None:
    service.enable(SET_ID, version=1)
    job = service.run()

    assert job.status is AutoEvaluationStatus.FAILED
    assert len(service.history()) == 1


def test_retry_is_append_only(
    service: AutoEvaluationService,
) -> None:
    service.enable(SET_ID, version=1)
    first = service.run()
    retry = service.retry(first.id)
    service.drain()

    history = service.history()
    assert len(history) == 2
    assert retry.id != first.id
    assert retry.parent_job_id == first.id
    assert service.show(first.id).finished_at == first.finished_at


def test_config_change_supersedes_queued_job(service: AutoEvaluationService) -> None:
    service.enable(SET_ID, version=1)
    queued = service.enqueue()
    service.disable()

    service.drain()

    finished = service.show(queued.id)
    assert finished.status is AutoEvaluationStatus.SUPERSEDED
    assert finished.error_code == "CONFIG_DISABLED"


def test_recover_invalidates_old_lease_and_prevents_late_write(
    service: AutoEvaluationService, session_factory: SessionFactory
) -> None:
    service.enable(SET_ID, version=1)
    queued = service.enqueue()
    claimed = service._claim_one()
    assert claimed is not None
    job_id, old_token = claimed
    with session_factory() as session:
        session.execute(
            update(AutoEvaluationJobRow)
            .where(AutoEvaluationJobRow.id == job_id)
            .values(lease_expires_at=(NOW - timedelta(seconds=1)).isoformat())
        )
        session.commit()

    assert service.recover() == 1
    assert (
        service._finalize(
            job_id,
            old_token,
            AutoEvaluationStatus.PASSED,
        )
        is False
    )
    assert service.show(queued.id).status is AutoEvaluationStatus.QUEUED


def test_claim_compare_and_swap_allows_only_one_worker(
    service: AutoEvaluationService,
) -> None:
    service.enable(SET_ID, version=1)
    queued = service.enqueue()

    first = service._claim_one()
    second = service._claim_one()

    assert first is not None and first[0] == queued.id
    assert second is None
