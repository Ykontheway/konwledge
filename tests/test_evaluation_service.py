from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from konwledge.domain import (
    Document,
    DocumentContentType,
    EvaluationItem,
    EvaluationSet,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.evaluation import (
    EvaluationService,
)
from konwledge.retrieval import RetrievalMode, RetrievalResult, RetrievalService
from konwledge.storage import (
    ImmutableEntityError,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

SET_ID = UUID("10000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("20000000-0000-0000-0000-000000000002")
SOURCE_ID = UUID("30000000-0000-0000-0000-000000000003")
DOC_ID = UUID("40000000-0000-0000-0000-000000000004")
UNIT_ID = UUID("50000000-0000-0000-0000-000000000005")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


@pytest.fixture
def session_factory() -> Iterator[SessionFactory]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    yield create_session_factory(database)
    database.dispose()


@pytest.fixture
def retrieval_service(session_factory: SessionFactory) -> RetrievalService:
    return RetrievalService(session_factory)


@pytest.fixture
def service(
    session_factory: SessionFactory, retrieval_service: RetrievalService
) -> EvaluationService:
    return EvaluationService(session_factory, retrieval_service, clock=lambda: NOW)


def setup_library(uow: UnitOfWork) -> None:
    uow.sources.add(Source(id=SOURCE_ID, kind=SourceKind.FILE, uri="test.md", created_at=NOW))
    uow.documents.add(
        Document(
            id=DOC_ID,
            source_id=SOURCE_ID,
            locator="test.md",
            content_type=DocumentContentType.TEXT,
            checksum="a" * 64,
            ingested_at=NOW,
        )
    )
    uow.knowledge_units.add(
        KnowledgeUnit(
            id=UNIT_ID,
            lineage_id=LINEAGE_ID,
            document_id=DOC_ID,
            ordinal=0,
            text="Evidence",
            start_char=0,
            end_char=8,
            status=KnowledgeUnitStatus.ACTIVE,
        )
    )


def make_set(version: int = 1, **changes: Any) -> EvaluationSet:
    item_changes = {}
    if "expected_lineage_ids" in changes:
        item_changes["expected_lineage_ids"] = changes.pop("expected_lineage_ids")

    item = EvaluationItem(
        id="case-1",
        query="Test query",
        expected_lineage_ids=item_changes.pop("expected_lineage_ids", frozenset({LINEAGE_ID})),
        **item_changes,
    )
    values = {
        "id": SET_ID,
        "name": "Test Set",
        "version": version,
        "created_at": NOW + timedelta(minutes=version),
        "items": (item,),
    }
    values.update(changes)
    return EvaluationSet.model_validate(values)


def test_import_set_idempotency_and_versioning(
    service: EvaluationService, session_factory: SessionFactory
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds1 = make_set(version=1)
    # First import
    res1 = service.import_set(ds1)
    assert res1.created is True
    assert res1.dataset == ds1

    # Idempotent re-import
    res2 = service.import_set(ds1)
    assert res2.created is False
    assert res2.dataset == ds1

    # Conflicting re-import (same version, different content)
    ds1_alt = ds1.model_copy(update={"name": "Different Name"})
    with pytest.raises(ImmutableEntityError):
        service.import_set(ds1_alt)

    # Next version
    ds2 = make_set(version=2)
    res3 = service.import_set(ds2)
    assert res3.created is True
    assert res3.dataset == ds2


def test_import_set_validation(service: EvaluationService, session_factory: SessionFactory) -> None:
    # Unknown lineage
    bad_ds = make_set(expected_lineage_ids=frozenset({uuid4()}))
    with pytest.raises(ValueError, match="unknown expected lineage IDs"):
        service.import_set(bad_ds)

    # First version must be 1
    bad_v = make_set(version=2)
    with pytest.raises(ValueError, match="first evaluation set version must be 1"):
        service.import_set(bad_v)

    # Contiguity
    ds1 = make_set(version=1)
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()
    service.import_set(ds1)

    ds3 = make_set(version=3)
    with pytest.raises(ValueError, match="contiguous"):
        service.import_set(ds3)

    # Time regression
    ds2_early = make_set(version=2, created_at=NOW - timedelta(days=1))
    with pytest.raises(ValueError, match="created_at must not regress"):
        service.import_set(ds2_early)


def test_run_evaluation(
    service: EvaluationService, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds = make_set()
    service.import_set(ds)

    # Mock retrieval to return a hit
    def mock_search(*args: Any, **kwargs: Any) -> RetrievalResult:
        # Actually hit.unit should be KnowledgeUnit
        unit = KnowledgeUnit(
            id=UNIT_ID,
            lineage_id=LINEAGE_ID,
            document_id=DOC_ID,
            ordinal=0,
            text="Evidence",
            start_char=0,
            end_char=8,
        )

        from konwledge.retrieval.models import RetrievalHit

        rhit = RetrievalHit(
            unit=unit,
            document=Document(
                id=DOC_ID,
                source_id=SOURCE_ID,
                locator="test.md",
                content_type=DocumentContentType.TEXT,
                checksum="a" * 64,
                ingested_at=NOW,
            ),
            source=Source(id=SOURCE_ID, kind=SourceKind.FILE, uri="test.md", created_at=NOW),
            status=KnowledgeUnitStatus.ACTIVE,
            score=1.0,
            lexical_rank=1,
        )
        return RetrievalResult(
            query="Test query",
            mode=RetrievalMode.LEXICAL,
            evaluated_at=NOW,
            include_stale=False,
            top_k=5,
            candidate_limit=50,
            rrf_k=60,
            hits=(rhit,),
        )

    monkeypatch.setattr(service._retrieval_service, "search", mock_search)

    report = service.run(SET_ID)
    assert report.dataset == ds
    assert len(report.results) == 1
    assert report.results[0].metrics.recall == 1.0
    assert report.metrics.mean_recall == 1.0
    assert report.metrics.case_count == 1
