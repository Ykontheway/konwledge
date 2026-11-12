from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

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
from konwledge.quality import QualityPolicy, QualityService
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


def test_quality_healthy_run(
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

        rhit = RetrievalHit(
            unit=KnowledgeUnit(
                id=UNIT_ID,
                lineage_id=LINEAGE_ID,
                document_id=DOC_ID,
                ordinal=0,
                text="Evidence unit text.",
                start_char=0,
                end_char=19,
            ),
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

    monkeypatch.setattr(quality_service._evaluation._retrieval_service, "search", mock_search)

    report = quality_service.run(SET_ID)
    assert report.passed is True
    assert report.metrics.mean_recall == 1.0
    assert any(c.name == "top_k_contract" and c.passed for c in report.checks)


def test_quality_metrics_failed(
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

    report = quality_service.run(SET_ID, policy=QualityPolicy(min_recall=0.9))
    assert report.passed is False
    assert any("recall_at_k" in v for v in report.violations)


def test_quality_wrong_top_k(
    quality_service: QualityService,
    session_factory: SessionFactory,
) -> None:
    with UnitOfWork(session_factory) as uow:
        setup_library(uow)
        uow.commit()

    ds = make_set()
    quality_service._evaluation.import_set(ds)

    report = quality_service.run(SET_ID, top_k=10, policy=QualityPolicy(required_top_k=5))
    assert report.passed is False
    assert any("top_k_contract" in v for v in report.violations)


def test_quality_no_answer_failure(
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
                id="no-answer-case",
                query="Absent query",
                expect_no_answer=True,
            ),
        )
    )
    quality_service._evaluation.import_set(ds)

    def mock_search(*args: Any, **kwargs: Any) -> RetrievalResult:
        from konwledge.retrieval.models import RetrievalHit

        rhit = RetrievalHit(
            unit=KnowledgeUnit(
                id=UNIT_ID,
                lineage_id=LINEAGE_ID,
                document_id=DOC_ID,
                ordinal=0,
                text="Irrelevant text.",
                start_char=0,
                end_char=16,
            ),
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
            score=0.5,
            lexical_rank=1,
        )
        return RetrievalResult(
            query="Absent query",
            mode=RetrievalMode.LEXICAL,
            evaluated_at=NOW,
            include_stale=False,
            top_k=5,
            candidate_limit=50,
            rrf_k=60,
            hits=(rhit,),
        )

    monkeypatch.setattr(quality_service._evaluation._retrieval_service, "search", mock_search)

    report = quality_service.run(SET_ID, policy=QualityPolicy(min_no_answer_accuracy=1.0))
    assert report.passed is False
    assert any("no_answer_accuracy" in v for v in report.violations)


def test_quality_stale_config_fail(
    quality_service: QualityService,
    session_factory: SessionFactory,
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

    report = quality_service.run(SET_ID, policy=QualityPolicy(allow_include_stale=False))
    assert report.passed is False
    assert any("stale_isolation_configuration" in v for v in report.violations)


def test_performance_percentile_slow_fail() -> None:
    from konwledge.quality.acceptance import populate_corpus, run_performance

    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    populate_corpus(session_factory)

    ticks = iter(float(value) for value in range(0, 124, 2))

    def slow_timer() -> float:
        return next(ticks)

    result = run_performance(session_factory, samples=30, p95_limit_seconds=1.0, timer=slow_timer)
    assert result.passed is False
    assert result.p95_seconds >= 2.0
    engine.dispose()


def test_installation_path_contract() -> None:
    """Ensure scripts/quality_acceptance.py runs correctly from any directory."""

    import konwledge

    repo_root = Path(konwledge.__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "quality_acceptance.py"

    with tempfile.TemporaryDirectory() as tmp_dir:
        cwd = Path(tmp_dir)
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{repo_root}/src:{env.get('PYTHONPATH', '')}"

        report_file = cwd / "report.json"
        result = subprocess.run(
            [sys.executable, str(script_path), "--report", str(report_file)],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode in (0, 1)
        if result.stdout:
            try:
                data = json.loads(result.stdout)
                assert "quality" in data
                assert "performance" in data
            except json.JSONDecodeError:
                pytest.fail(f"Script output not valid JSON: {result.stdout}")


def test_cli_report_safe_write(tmp_path: Path) -> None:
    from konwledge.newcli_quality import quality_app

    runner = CliRunner()
    report = tmp_path / "report.json"
    report.write_text("existing", encoding="utf-8")

    result = runner.invoke(quality_app, ["acceptance", "--report", str(report)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined.lower()
    assert report.read_text(encoding="utf-8") == "existing"


def test_cli_no_traceback_on_validation_error(tmp_path: Path) -> None:
    from konwledge.newcli_quality import quality_app

    runner = CliRunner()
    policy = tmp_path / "policy.json"
    policy.write_text('{"min_recall": "invalid"}', encoding="utf-8")

    result = runner.invoke(
        quality_app,
        ["run", "--set-id", str(SET_ID), "--policy-json", str(policy)],
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.stderr
    combined = result.stdout + (result.stderr or "")
    assert "failed to load quality policy" in combined.lower()


def test_quality_regression_incompatible_baseline() -> None:
    from konwledge.quality.acceptance import (
        populate_corpus,
        run_quality,
    )
    from konwledge.quality.benchmark_corpus import (
        EVALUATED_AT,
    )
    from konwledge.quality.benchmark_corpus import (
        SET_ID as CORPUS_SET_ID,
    )

    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    populate_corpus(session_factory)
    healthy = run_quality(session_factory)
    incompatible = healthy.baseline.model_copy(update={"mode": RetrievalMode.VECTOR})

    report = QualityService(
        session_factory,
        RetrievalService(session_factory, clock=lambda: EVALUATED_AT),
        clock=lambda: EVALUATED_AT,
    ).run(CORPUS_SET_ID, baseline=incompatible)

    assert report.passed is False
    assert any("regression_gate" in violation for violation in report.violations)
    engine.dispose()
