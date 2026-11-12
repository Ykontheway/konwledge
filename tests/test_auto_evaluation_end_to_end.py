from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.engine import Engine

from konwledge.api.app import create_app
from konwledge.application import ImportReport, KnowledgeLibrary
from konwledge.auto_evaluation import AutoEvaluationJob, AutoEvaluationStatus
from konwledge.domain import EvaluationItem, EvaluationSet, FreshnessPolicy
from konwledge.evaluation import EvaluationBaseline, GatePolicy
from konwledge.lifecycle import materialize_statuses, publish_document
from konwledge.quality import QualityPolicy, QualityReport
from konwledge.retrieval import RetrievalMode
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.tables import EvaluationSetRow

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
ANSWER_TOKEN = "azurequartz"
NO_ANSWER_TOKEN = "zzznomatchsentinel"
INITIAL_MARKDOWN = "# Operations\nThe launch code is azurequartz and the verified owner is Mira.\n"
PASSING_UPDATE = (
    "# Operations\n"
    "The launch code is azurequartz and the verified owner is Mira.\n"
    "This statement was reviewed again.\n"
)
FAILING_UPDATE = (
    "# Operations\nThe launch code was removed; contact the operations desk for verification.\n"
)
_SAFE_ERROR_CODE = re.compile(r"^[A-Z0-9_]+$")


@dataclass
class RealLibrary:
    database_path: Path
    markdown_path: Path
    engine: Engine
    session_factory: SessionFactory
    library: KnowledgeLibrary
    now: list[datetime]
    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    unit_id: UUID
    lineage_id: UUID


@pytest.fixture
def real_library(tmp_path: Path) -> Iterator[RealLibrary]:
    database_path = tmp_path / "knowledge.db"
    markdown_path = tmp_path / "operations.md"
    markdown_path.write_text(INITIAL_MARKDOWN, encoding="utf-8")
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    now = [NOW]
    library = KnowledgeLibrary(session_factory, clock=lambda: now[0])

    imported = library.import_path(markdown_path).imported[0].result
    assert imported.ingestion.created is True
    assert len(imported.units) == 1
    detail = library.show_unit(imported.units[0].id)

    dataset_id = uuid4()
    dataset = EvaluationSet(
        id=dataset_id,
        name="real imported operations evidence",
        version=1,
        created_at=now[0],
        items=(
            EvaluationItem(
                id="answer-from-imported-markdown",
                query=ANSWER_TOKEN,
                expected_lineage_ids=frozenset({detail.unit.lineage_id}),
            ),
            EvaluationItem(
                id="real-no-answer",
                query=NO_ANSWER_TOKEN,
                expect_no_answer=True,
            ),
        ),
    )
    imported_set = library.evaluation.import_set(dataset)
    assert imported_set.created is True

    scenario = RealLibrary(
        database_path=database_path,
        markdown_path=markdown_path,
        engine=engine,
        session_factory=session_factory,
        library=library,
        now=now,
        dataset_id=dataset_id,
        source_id=detail.source.id,
        document_id=detail.document.id,
        unit_id=detail.unit.id,
        lineage_id=detail.unit.lineage_id,
    )
    try:
        yield scenario
    finally:
        engine.dispose()


def _enable(scenario: RealLibrary) -> None:
    config = scenario.library.automatic_evaluation.enable(scenario.dataset_id, version=1)
    assert config.quality_policy == QualityPolicy()
    assert config.top_k == 5


def _import_update(scenario: RealLibrary, content: str) -> ImportReport:
    scenario.now[0] += timedelta(seconds=1)
    scenario.markdown_path.write_text(content, encoding="utf-8")
    return scenario.library.import_path(scenario.markdown_path)


def _only_job(scenario: RealLibrary) -> AutoEvaluationJob:
    jobs = scenario.library.automatic_evaluation.history()
    assert len(jobs) == 1
    return jobs[0]


def test_default_is_closed_and_dataset_uses_real_imported_lineage(
    real_library: RealLibrary,
) -> None:
    service = real_library.library.automatic_evaluation
    dataset = real_library.library.evaluation.get(real_library.dataset_id, version=1)

    assert service.status() is None
    assert dataset.items[0].expected_lineage_ids == frozenset({real_library.lineage_id})
    assert real_library.library.search(ANSWER_TOKEN).hits[0].unit.id == real_library.unit_id

    _import_update(real_library, PASSING_UPDATE)
    assert service.history() == ()


def test_enable_pins_defaults_and_unchanged_import_creates_no_job(
    real_library: RealLibrary,
) -> None:
    service = real_library.library.automatic_evaluation
    _enable(real_library)
    pinned = service.status()
    assert pinned is not None
    assert pinned.dataset_id == real_library.dataset_id
    assert pinned.dataset_version == 1
    assert len(pinned.dataset_hash) == 64

    repeated = real_library.library.import_path(real_library.markdown_path).imported[0].result

    assert repeated.ingestion.created is False
    assert repeated.diff is None
    assert repeated.units_created is False
    assert service.history() == ()


def test_changed_file_runs_real_query_and_has_one_healthy_pass(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    _import_update(real_library, PASSING_UPDATE)

    job = _only_job(real_library)
    assert job.status is AutoEvaluationStatus.PASSED
    assert job.error_code is None
    assert job.trigger_revision_ids
    assert job.report is not None
    assert job.report["passed"] is True
    assert job.report["metrics"]["mean_recall"] == 1.0
    assert job.report["metrics"]["mean_reciprocal_rank"] == 1.0
    assert job.report["metrics"]["no_answer_accuracy"] == 1.0
    assert job.report["citation_coverage"] == 1.0
    assert job.report["stale_hits"] == 0
    hit = real_library.library.search(ANSWER_TOKEN).hits[0]
    assert hit.unit.lineage_id == real_library.lineage_id
    assert "reviewed again" in hit.unit.text


def test_failed_gate_does_not_rollback_knowledge_and_error_is_safe(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    _import_update(real_library, FAILING_UPDATE)

    job = _only_job(real_library)
    assert job.status is AutoEvaluationStatus.FAILED
    assert job.error_code == "QUALITY_GATE_FAILED"
    assert _SAFE_ERROR_CODE.fullmatch(job.error_code)
    assert job.report is not None and job.report["passed"] is False
    assert real_library.library.search("operations desk").hits
    assert real_library.library.search(ANSWER_TOKEN).hits == ()
    assert "operations desk" not in str(job.report)
    assert str(real_library.markdown_path) not in str(job.report)


def test_new_session_factory_and_library_retain_config_and_trigger(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)
    real_library.engine.dispose()

    restarted_engine = create_sqlite_engine(f"sqlite+pysqlite:///{real_library.database_path}")
    create_schema(restarted_engine)
    restarted_factory = create_session_factory(restarted_engine)
    restarted = KnowledgeLibrary(restarted_factory, clock=lambda: real_library.now[0])
    try:
        config = restarted.automatic_evaluation.status()
        assert config is not None and config.enabled is True
        assert config.dataset_id == real_library.dataset_id

        real_library.now[0] += timedelta(seconds=1)
        real_library.markdown_path.write_text(PASSING_UPDATE, encoding="utf-8")
        restarted.import_path(real_library.markdown_path)

        jobs = restarted.automatic_evaluation.history()
        assert len(jobs) == 1
        assert jobs[0].status is AutoEvaluationStatus.PASSED
    finally:
        restarted_engine.dispose()


def test_lifecycle_mark_stale_and_refresh_both_trigger_real_evaluation(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    real_library.now[0] += timedelta(seconds=1)
    real_library.library.lifecycle.mark_stale(
        real_library.unit_id, reason="source requires review", author="e2e"
    )
    after_stale = real_library.library.automatic_evaluation.history()
    assert len(after_stale) == 1
    assert after_stale[0].status is AutoEvaluationStatus.FAILED
    assert real_library.library.search(ANSWER_TOKEN).hits == ()

    real_library.now[0] += timedelta(seconds=1)
    real_library.library.lifecycle.refresh(
        real_library.unit_id, reason="source reverified", author="e2e"
    )
    jobs = real_library.library.automatic_evaluation.history()
    assert len(jobs) == 2
    assert jobs[0].status is AutoEvaluationStatus.PASSED
    assert jobs[0].trigger_revision_ids
    assert real_library.library.search(ANSWER_TOKEN).hits


def test_freshness_policy_and_applying_scan_really_trigger(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    real_library.library.freshness.configure(
        real_library.source_id, FreshnessPolicy(ttl_seconds=60)
    )
    configured = real_library.library.automatic_evaluation.history()
    assert len(configured) == 1
    assert configured[0].status is AutoEvaluationStatus.PASSED

    real_library.now[0] += timedelta(seconds=61)
    scan = real_library.library.freshness.scan(dry_run=False)

    assert len(scan.marked) == 1
    jobs = real_library.library.automatic_evaluation.history()
    assert len(jobs) == 2
    assert jobs[0].status is AutoEvaluationStatus.FAILED
    assert jobs[0].error_code == "QUALITY_GATE_FAILED"


def test_conversation_approval_triggers_once_and_idempotent_approve_does_not_repeat(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)
    imported = real_library.library.conversations.import_conversation(
        {
            "schema_version": 1,
            "conversation_id": "auto-eval-e2e-conversation",
            "version": 1,
            "messages": [
                {
                    "id": "message-1",
                    "role": "user",
                    "content": "Fact: The unrelated support window starts at noon.",
                }
            ],
        }
    )
    candidate = imported.candidates[0]
    assert real_library.library.automatic_evaluation.history() == ()

    first = real_library.library.conversations.approve(
        candidate.id, "reviewer-e2e", reason="verified"
    )
    jobs_after_first = real_library.library.automatic_evaluation.history()
    second = real_library.library.conversations.approve(candidate.id, "other-reviewer")

    assert first.id == second.id
    assert len(jobs_after_first) == 1
    assert jobs_after_first[0].status is AutoEvaluationStatus.PASSED
    assert len(real_library.library.automatic_evaluation.history()) == 1
    assert real_library.library.search("support window").hits


def test_external_sqlite_schema_and_alter_trigger_without_row_or_path_leaks(
    real_library: RealLibrary, tmp_path: Path
) -> None:
    _enable(real_library)
    secret = "TOP-SECRET-ROW-VALUE"
    external = tmp_path / "user-password-private.sqlite"
    connection = sqlite3.connect(external)
    try:
        connection.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY, password TEXT NOT NULL)")
        connection.execute("INSERT INTO accounts(password) VALUES (?)", (secret,))
        connection.commit()
    finally:
        connection.close()

    first = real_library.library.database_schema.capture_sqlite(
        external, title="External account schema"
    )
    assert first.created is True
    assert "accounts" in first.snapshot.canonical_json()
    assert secret not in first.snapshot.canonical_json()

    connection = sqlite3.connect(external)
    try:
        connection.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
        connection.commit()
    finally:
        connection.close()
    real_library.now[0] += timedelta(seconds=1)
    second = real_library.library.database_schema.capture_sqlite(
        external, title="External account schema"
    )

    jobs = real_library.library.automatic_evaluation.history()
    assert second.version == 2
    assert second.diff is not None
    assert len(jobs) == 2
    assert all(job.status is AutoEvaluationStatus.PASSED for job in jobs)
    stored = repr(real_library.library.list_documents()) + repr(jobs)
    assert secret not in stored
    assert str(external) not in stored
    assert "user-password-private.sqlite" not in stored


def test_enabled_rolled_back_publication_creates_no_job(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    with UnitOfWork(real_library.session_factory) as uow:
        document = uow.documents.get(real_library.document_id)
        assert document is not None
        publish_document(
            uow,
            document,
            previous_document_id=document.id,
            reverted=True,
            id_factory=uuid4,
            created_at=real_library.now[0] + timedelta(seconds=1),
        )
        materialize_statuses(uow, real_library.source_id)
        uow.rollback()

    assert real_library.library.automatic_evaluation.history() == ()
    assert real_library.library.search(ANSWER_TOKEN).hits


def test_retrieval_evaluation_quality_and_http_reads_do_not_enqueue(
    real_library: RealLibrary,
) -> None:
    _enable(real_library)

    assert real_library.library.search(ANSWER_TOKEN).hits
    evaluation = real_library.library.evaluation.run(real_library.dataset_id, version=1)
    quality = real_library.library.quality.run(real_library.dataset_id, version=1)
    with TestClient(create_app(real_library.library)) as client:
        response = client.post("/v1/context", json={"query": ANSWER_TOKEN})

    assert evaluation.metrics.mean_recall == 1.0
    assert quality.passed is True
    assert response.status_code == 200
    assert response.json()["blocks"]
    assert real_library.library.automatic_evaluation.history() == ()


def test_knowledge_change_during_real_quality_run_supersedes_old_job(
    real_library: RealLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(real_library)
    service = real_library.library.automatic_evaluation
    original_run = service._quality.run
    injected = False

    def run_then_change(
        set_id: UUID,
        *,
        version: int | None = None,
        policy: QualityPolicy | None = None,
        baseline: EvaluationBaseline | None = None,
        regression_policy: GatePolicy | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
    ) -> QualityReport:
        nonlocal injected
        report = original_run(
            set_id,
            version=version,
            policy=policy,
            baseline=baseline,
            regression_policy=regression_policy,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            at=at,
        )
        if not injected:
            injected = True
            real_library.now[0] += timedelta(seconds=1)
            real_library.markdown_path.write_text(
                PASSING_UPDATE + "A concurrent reviewer changed this file.\n",
                encoding="utf-8",
            )
            real_library.library.import_path(real_library.markdown_path)
        return report

    monkeypatch.setattr(service._quality, "run", run_then_change)

    _import_update(real_library, PASSING_UPDATE)

    jobs = service.history()
    assert len(jobs) == 2
    superseded = next(job for job in jobs if job.error_code == "KNOWLEDGE_CHANGED")
    assert superseded.status is AutoEvaluationStatus.SUPERSEDED
    assert superseded.report is not None and superseded.report["passed"] is True
    assert any(job.status is AutoEvaluationStatus.PASSED for job in jobs)


def test_config_change_during_real_quality_run_cannot_finalize_passed(
    real_library: RealLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(real_library)
    service = real_library.library.automatic_evaluation
    original_run = service._quality.run

    def run_then_reconfigure(
        set_id: UUID,
        *,
        version: int | None = None,
        policy: QualityPolicy | None = None,
        baseline: EvaluationBaseline | None = None,
        regression_policy: GatePolicy | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
    ) -> QualityReport:
        report = original_run(
            set_id,
            version=version,
            policy=policy,
            baseline=baseline,
            regression_policy=regression_policy,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            at=at,
        )
        service.enable(real_library.dataset_id, version=1)
        return report

    monkeypatch.setattr(service._quality, "run", run_then_reconfigure)

    _import_update(real_library, PASSING_UPDATE)

    job = _only_job(real_library)
    assert job.report is not None and job.report["passed"] is True
    assert job.status is AutoEvaluationStatus.SUPERSEDED
    assert job.error_code == "CONFIG_CHANGED"


def test_dataset_pin_tamper_during_real_quality_run_cannot_finalize_passed(
    real_library: RealLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(real_library)
    service = real_library.library.automatic_evaluation
    original_run = service._quality.run

    def run_then_tamper(
        set_id: UUID,
        *,
        version: int | None = None,
        policy: QualityPolicy | None = None,
        baseline: EvaluationBaseline | None = None,
        regression_policy: GatePolicy | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
    ) -> QualityReport:
        report = original_run(
            set_id,
            version=version,
            policy=policy,
            baseline=baseline,
            regression_policy=regression_policy,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            at=at,
        )
        with real_library.session_factory() as session:
            session.execute(
                update(EvaluationSetRow)
                .where(
                    EvaluationSetRow.id == real_library.dataset_id,
                    EvaluationSetRow.version == 1,
                )
                .values(content_hash="f" * 64)
            )
            session.commit()
        return report

    monkeypatch.setattr(service._quality, "run", run_then_tamper)

    _import_update(real_library, PASSING_UPDATE)

    job = _only_job(real_library)
    assert job.report is not None and job.report["passed"] is True
    assert job.status is AutoEvaluationStatus.BLOCKED
    assert job.error_code == "INTEGRITY_MISMATCH"
    assert _SAFE_ERROR_CODE.fullmatch(job.error_code)
