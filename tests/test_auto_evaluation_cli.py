"""Scoped automatic-evaluation observation and CLI-notice contract tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from konwledge.application import KnowledgeLibrary
from konwledge.auto_evaluation import AutoEvaluationStatus
from konwledge.auto_evaluation.notifications import notify_cli
from konwledge.domain import (
    EvaluationItem,
    EvaluationSet,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
PASSING = "# Operations\nThe launch token is scoped-observer-token.\n"
PASSING_UPDATE = PASSING + "This fact was reviewed again.\n"
FAILING_UPDATE = "# Operations\nThe launch token was removed; use fallback-desk-token.\n"


@dataclass
class Scenario:
    library: KnowledgeLibrary
    session_factory: SessionFactory
    database: Path
    markdown: Path
    now: list[datetime]
    dataset_id: UUID
    scenario_source_id: UUID
    lineage_id: UUID


@pytest.fixture
def scenario(tmp_path: Path) -> Iterator[Scenario]:
    database = tmp_path / "knowledge.db"
    markdown = tmp_path / "operations.md"
    markdown.write_text(PASSING, encoding="utf-8")
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    now = [NOW]
    library = KnowledgeLibrary(session_factory, clock=lambda: now[0])
    imported = library.import_path(markdown).imported[0].result
    detail = library.show_unit(imported.units[0].id)
    dataset_id = uuid4()
    library.evaluation.import_set(
        EvaluationSet(
            id=dataset_id,
            name="scoped observer fixture",
            version=1,
            created_at=now[0],
            items=(
                EvaluationItem(
                    id="launch-token",
                    query="scoped-observer-token",
                    expected_lineage_ids=frozenset({detail.unit.lineage_id}),
                ),
            ),
        )
    )
    try:
        yield Scenario(
            library,
            session_factory,
            database,
            markdown,
            now,
            dataset_id,
            detail.source.id,
            detail.unit.lineage_id,
        )
    finally:
        engine.dispose()


def _update(scenario: Scenario, content: str) -> None:
    scenario.now[0] += timedelta(seconds=1)
    scenario.markdown.write_text(content, encoding="utf-8")
    scenario.library.import_path(scenario.markdown)


def test_disabled_and_rolled_back_changes_produce_no_observation(scenario: Scenario) -> None:
    service = scenario.library.automatic_evaluation
    with service.observe_changes() as disabled:
        _update(scenario, PASSING_UPDATE)
    assert disabled.job_ids == ()
    assert disabled.jobs == ()

    service.enable(scenario.dataset_id, version=1)
    with service.observe_changes() as rolled_back, UnitOfWork(scenario.session_factory) as uow:
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=scenario.lineage_id,
                revision=99,
                change_type=RevisionChangeType.UPDATE,
                created_at=scenario.now[0],
                payload={"change": "refreshed", "content_hash": "f" * 64},
            )
        )
        uow.rollback()
    assert rolled_back.jobs == ()


def test_passing_change_is_observed_and_has_summary(scenario: Scenario) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)

    with service.watch_changes() as observed:
        _update(scenario, PASSING_UPDATE)

    assert len(observed.job_ids) == 1
    assert observed.jobs[0].id == observed.job_ids[0]
    assert observed.jobs[0].status is AutoEvaluationStatus.PASSED
    stderr: list[str] = []
    assert notify_cli(observed.jobs, stderr=stderr.append) is False
    assert stderr == [
        "Automatic evaluation: total=1 queued=0 running=0 passed=1 failed=0 blocked=0 superseded=0"
    ]


def test_failed_change_is_committed_and_cli_notice_requests_exit_three(
    scenario: Scenario,
) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)

    with service.observe_changes() as observed:
        _update(scenario, FAILING_UPDATE)

    job = observed.jobs[0]
    assert job.status is AutoEvaluationStatus.FAILED
    assert job.error_code == "QUALITY_GATE_FAILED"
    assert scenario.library.search("fallback-desk-token").hits
    assert scenario.library.search("scoped-observer-token").hits == ()
    stderr: list[str] = []
    assert notify_cli(observed.jobs, stderr=stderr.append) is True
    assert "knowledge committed; evaluation did not pass" in stderr[-1]
    assert str(job.id) in stderr[-1]


def test_nested_observers_share_only_jobs_within_their_scopes(scenario: Scenario) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)

    with service.observe_changes() as outer:
        with service.observe_changes() as inner:
            _update(scenario, PASSING_UPDATE)
        inner_ids = inner.job_ids

    assert inner_ids
    assert outer.job_ids == inner_ids
    assert tuple(job.id for job in outer.jobs) == inner_ids


def test_history_and_manual_jobs_are_not_mistaken_for_new_domain_jobs(
    scenario: Scenario,
) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)
    _update(scenario, FAILING_UPDATE)
    historical_failure = service.history(limit=1)[0]
    assert historical_failure.status is AutoEvaluationStatus.FAILED

    with service.observe_changes() as read_only:
        assert service.history(limit=1)[0].id == historical_failure.id
        service.status()
    assert read_only.jobs == ()

    with service.observe_changes() as manual:
        manual_job = service.run()
    assert manual.jobs == ()
    assert manual_job.id != historical_failure.id

    with service.observe_changes() as current:
        _update(scenario, PASSING_UPDATE)
    assert len(current.jobs) == 1
    assert current.jobs[0].id not in {historical_failure.id, manual_job.id}
    assert current.jobs[0].status is AutoEvaluationStatus.PASSED


def test_json_stdout_stays_machine_readable_and_failure_warning_is_stderr_only(
    scenario: Scenario,
) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)
    with service.observe_changes() as observed:
        _update(scenario, FAILING_UPDATE)

    existing_stdout = json.dumps({"created": True, "version": 2})
    stderr: list[str] = []
    assert notify_cli(observed.jobs, stderr=stderr.append, json_output=True) is True
    assert json.loads(existing_stdout) == {"created": True, "version": 2}
    assert len(stderr) == 2
    assert stderr[0].startswith("Automatic evaluation: total=1")
    assert stderr[1].startswith("Warning: knowledge committed; evaluation did not pass")


def test_duplicate_triggered_jobs_are_not_observed_multiple_times(scenario: Scenario) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)

    with service.observe_changes() as observer:
        # A single domain operation triggering multiple identical jobs
        # (simulated by manual injection of same ID to observer list)
        job_id = uuid4()
        with scenario.session_factory() as session:
            # First commit of the ID
            session.info.setdefault("auto_evaluation_observed_job_ids", []).append(job_id)
            session.commit()
            # Second "accidental" commit or observation of same ID
            session.info.setdefault("auto_evaluation_observed_job_ids", []).append(job_id)
            session.commit()

    assert observer.job_ids == (job_id,)


def test_rollback_after_observed_commit_does_not_leak_ids(scenario: Scenario) -> None:
    service = scenario.library.automatic_evaluation
    service.enable(scenario.dataset_id, version=1)

    with service.observe_changes() as observer:
        # Commit something
        _update(scenario, PASSING_UPDATE)
        assert len(observer.job_ids) == 1

        # Failed transaction afterward
        with pytest.raises(IntegrityError), scenario.session_factory() as session:
            session.info.setdefault("auto_evaluation_observed_job_ids", []).append(uuid4())
            # Trigger a real DB error to force rollback (duplicate key)
            session.execute(
                text(
                    "INSERT INTO sources (id, kind, uri, created_at) "
                    "VALUES (:id, 'file', 'dup.md', '2026-11-12')"
                ),
                {"id": str(scenario.scenario_source_id)},
            )
            session.commit()

    assert len(observer.job_ids) == 1
