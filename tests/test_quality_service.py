from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

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
from konwledge.quality import QualityService
from konwledge.retrieval import RetrievalMode, RetrievalResult, RetrievalService
from konwledge.storage import (
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
def quality_service(
    session_factory: SessionFactory, retrieval_service: RetrievalService
) -> QualityService:
    return QualityService(session_factory, retrieval_service, clock=lambda: NOW)


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
            text="Evidence unit text.",
            start_char=0,
            end_char=19,
            status=KnowledgeUnitStatus.ACTIVE,
        )
    )


def make_set(version: int = 1, **changes: Any) -> EvaluationSet:
    item = EvaluationItem(
        id="case-1",
        query="Test query",
        expected_lineage_ids=frozenset({LINEAGE_ID}),
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


def test_quality_run_passing(
    quality_service: QualityService,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds = make_set()
    quality_service._evaluation.import_set(ds)

    def mock_search(*args: Any, **kwargs: Any) -> RetrievalResult:
        from konwledge.retrieval.models import RetrievalHit

        unit = KnowledgeUnit(
            id=UNIT_ID,
            lineage_id=LINEAGE_ID,
            document_id=DOC_ID,
            ordinal=0,
            text="Evidence unit text.",
            start_char=0,
            end_char=19,
        )
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

    # We need to monkeypatch the internal retrieval service's search
    monkeypatch.setattr(quality_service._evaluation._retrieval_service, "search", mock_search)

    report = quality_service.run(SET_ID)
    assert report.passed is True
    assert report.citation_coverage == 1.0
    assert report.stale_hits == 0
    assert report.metrics.mean_recall == 1.0


def test_quality_run_failing_recall(
    quality_service: QualityService,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds = make_set()
    quality_service._evaluation.import_set(ds)

    def mock_search(*args: Any, **kwargs: Any) -> RetrievalResult:
        return RetrievalResult(
            query="Test query",
            mode=RetrievalMode.LEXICAL,
            evaluated_at=NOW,
            include_stale=False,
            top_k=5,
            candidate_limit=50,
            rrf_k=60,
            hits=(),
        )

    monkeypatch.setattr(quality_service._evaluation._retrieval_service, "search", mock_search)

    report = quality_service.run(SET_ID)
    assert report.passed is False
    assert any("recall_at_k" in v for v in report.violations)


def test_quality_run_stale_rejection(
    quality_service: QualityService,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds = make_set(
        items=(
            EvaluationItem(
                id="c1",
                query="q",
                expected_lineage_ids=frozenset({LINEAGE_ID}),
                include_stale=True,
            ),
        )
    )
    quality_service._evaluation.import_set(ds)

    report = quality_service.run(SET_ID)
    assert report.passed is False
    assert any("stale_isolation_configuration" in v for v in report.violations)
