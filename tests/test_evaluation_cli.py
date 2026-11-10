"""Evaluation CLI acceptance tests using isolated file databases and pure JSON streams."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from typer.testing import CliRunner, Result

from konwledge import cli
from konwledge.application import KnowledgeLibrary
from konwledge.domain import EvaluationItem
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider
from konwledge.evaluation import EvaluationReport, EvaluationSet, dump_evaluation_set
from konwledge.indexing import IndexingService, InMemoryVectorIndex
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

runner = CliRunner()


def invoke(*arguments: str) -> Result:
    return runner.invoke(cli.app, list(arguments))


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, KnowledgeLibrary, UUID, UUID]:
    database = tmp_path / "数据库 with spaces" / "knowledge.db"
    database.parent.mkdir()
    source = tmp_path / "中文 corpus.md"
    source.write_text("# Guide\nservice listens on target port.\n", encoding="utf-8")
    imported = invoke("import", str(source), "-d", str(database))
    assert imported.exit_code == 0, imported.stderr

    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    library = KnowledgeLibrary(create_session_factory(engine))
    summary = library.list_documents()[0]
    engine.dispose()
    return database, library, summary.units[0].lineage_id, summary.source.id


def make_dataset(
    lineage_id: UUID,
    source_id: UUID,
    *,
    set_id: UUID | None = None,
    version: int = 1,
    created_at: datetime | None = None,
) -> EvaluationSet:
    return EvaluationSet(
        id=set_id or uuid4(),
        name="CLI evaluation 中文",
        version=version,
        created_at=created_at or datetime(2026, 11, 10, tzinfo=UTC),
        items=(
            EvaluationItem(
                id="answerable",
                query="target",
                expected_lineage_ids=frozenset({lineage_id}),
                source_id=source_id,
            ),
            EvaluationItem(
                id="no-answer",
                query="definitely-absent-token",
                expected_lineage_ids=frozenset(),
                expect_no_answer=True,
                source_id=source_id,
            ),
        ),
    )


def write_dataset(path: Path, dataset: EvaluationSet) -> None:
    path.write_text(dump_evaluation_set(dataset), encoding="utf-8")


def table_rows(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        with engine.connect() as connection:
            names = (
                "sources",
                "documents",
                "knowledge_units",
                "revisions",
                "citations",
                "conflicts",
                "evaluation_sets",
            )
            return {
                name: tuple(
                    tuple(row)
                    for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY rowid"))
                )
                for name in names
            }
    finally:
        engine.dispose()


def test_malformed_import_is_rejected_before_database_creation(tmp_path: Path) -> None:
    malformed = tmp_path / "坏 evaluation.json"
    malformed.write_text('{"schema_version": 1, "items": [}', encoding="utf-8")
    database = tmp_path / "must not exist" / "knowledge.db"

    result = invoke("evaluation", "import", str(malformed), "--json", "-d", str(database))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "could not parse evaluation set" in result.stderr
    assert not database.exists() and not database.parent.exists()


def test_import_list_show_export_run_and_reports_are_read_only(
    corpus: tuple[Path, KnowledgeLibrary, UUID, UUID], tmp_path: Path
) -> None:
    database, _, lineage_id, source_id = corpus
    dataset = make_dataset(lineage_id, source_id)
    path = tmp_path / "evaluation 中文.json"
    write_dataset(path, dataset)

    imported = invoke("evaluation", "import", str(path), "--json", "-d", str(database))
    assert imported.exit_code == 0, imported.stderr
    assert imported.stderr == ""
    assert EvaluationSet.model_validate_json(imported.stdout) == dataset
    assert imported.stdout == dump_evaluation_set(dataset)

    idempotent = invoke("evaluation", "import", str(path), "-d", str(database))
    assert idempotent.exit_code == 0 and idempotent.stdout.startswith("existing:")

    listed = invoke("evaluation", "list", "--json", "-d", str(database))
    assert listed.exit_code == 0 and listed.stderr == ""
    listed_json: list[dict[str, Any]] = json.loads(listed.stdout)
    assert [entry["id"] for entry in listed_json] == [str(dataset.id)]

    shown = invoke("evaluation", "show", str(dataset.id), "--version", "1", "-d", str(database))
    assert shown.exit_code == 0 and shown.stdout == dump_evaluation_set(dataset)

    exported_path = tmp_path / "exported canonical.json"
    exported = invoke(
        "evaluation",
        "export",
        str(dataset.id),
        "--version",
        "1",
        "--output",
        str(exported_path),
        "-d",
        str(database),
    )
    assert exported.exit_code == 0
    assert exported_path.read_text(encoding="utf-8") == dump_evaluation_set(dataset)

    before = table_rows(database)
    run = invoke(
        "evaluation",
        "run",
        str(dataset.id),
        "--version",
        "1",
        "--top-k",
        "3",
        "--json",
        "-d",
        str(database),
    )
    assert run.exit_code == 0, run.stderr
    assert run.stderr == ""
    report = EvaluationReport.model_validate_json(run.stdout)
    assert report.dataset == dataset and report.dataset_hash == dataset.content_hash
    assert report.metrics.case_count == 2
    assert report.metrics.answerable_count == report.metrics.no_answer_count == 1
    assert report.metrics.mean_recall == 1.0
    assert report.metrics.mean_precision == pytest.approx(1 / 3)
    assert report.metrics.no_answer_accuracy == 1.0
    assert [case.item_id for case in report.results] == ["answerable", "no-answer"]
    assert len(report.results[0].retrieval.hits) == 1
    assert report.results[1].retrieval.hits == ()
    assert table_rows(database) == before

    human = invoke("evaluation", "run", str(dataset.id), "-d", str(database))
    assert human.exit_code == 0 and human.stderr == ""
    assert "Evaluation: CLI evaluation 中文 v1" in human.stdout
    assert "status=answerable" in human.stdout and "status=no-answer" in human.stdout
    assert "no_answer=n/a" in human.stdout and "no_answer=true" in human.stdout
    assert "degraded=false" in human.stdout


def test_contiguous_history_latest_list_and_conflicting_immutable_input(
    corpus: tuple[Path, KnowledgeLibrary, UUID, UUID], tmp_path: Path
) -> None:
    database, _, lineage_id, source_id = corpus
    first = make_dataset(lineage_id, source_id)
    first_path = tmp_path / "v1.json"
    write_dataset(first_path, first)
    assert invoke("evaluation", "import", str(first_path), "-d", str(database)).exit_code == 0

    conflict = first.model_copy(update={"description": "changed in place"})
    conflict_path = tmp_path / "conflict.json"
    write_dataset(conflict_path, conflict)
    rejected = invoke("evaluation", "import", str(conflict_path), "-d", str(database))
    assert rejected.exit_code == 1
    assert "immutable" in rejected.stderr and rejected.stdout == ""

    second = make_dataset(
        lineage_id,
        source_id,
        set_id=first.id,
        version=2,
        created_at=datetime(2026, 11, 11, tzinfo=UTC),
    )
    second_path = tmp_path / "v2.json"
    write_dataset(second_path, second)
    assert invoke("evaluation", "import", str(second_path), "-d", str(database)).exit_code == 0

    history = invoke("evaluation", "history", str(first.id), "--json", "-d", str(database))
    versions = [entry["version"] for entry in json.loads(history.stdout)]
    assert history.exit_code == 0 and versions == [1, 2]
    latest = json.loads(invoke("evaluation", "list", "--json", "-d", str(database)).stdout)
    assert latest[0]["version"] == 2


def test_unknown_gold_is_runtime_failure(
    corpus: tuple[Path, KnowledgeLibrary, UUID, UUID], tmp_path: Path
) -> None:
    database, _, _, source_id = corpus
    dataset = make_dataset(uuid4(), source_id)
    path = tmp_path / "unknown-gold.json"
    write_dataset(path, dataset)

    result = invoke("evaluation", "import", str(path), "--json", "-d", str(database))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unknown expected lineage IDs" in result.stderr


@pytest.mark.parametrize(
    "options,diagnostic",
    [
        (("--top-k", "0"), "--top-k"),
        (("--candidate-limit", "0"), "--candidate-limit"),
        (("--rrf-k", "0"), "--rrf-k"),
        (("--at", "not-a-time"), "--at"),
        (("--mode", "lexical", "--demo-vectors"), "--demo-vectors"),
        (("--top-k", "5", "--candidate-limit", "4"), "candidate_limit"),
    ],
)
def test_invalid_run_options_exit_two_before_database_creation(
    tmp_path: Path, options: tuple[str, ...], diagnostic: str
) -> None:
    database = tmp_path / "unopened" / "knowledge.db"
    result = invoke("evaluation", "run", str(uuid4()), *options, "--json", "-d", str(database))
    assert result.exit_code == 2
    assert result.stdout == "" and diagnostic in result.stderr
    assert not database.exists()


def test_demo_vectors_index_historical_versions_once_and_keep_json_pure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.db"
    source = tmp_path / "versioned.md"
    source.write_text("# Guide\nhistorical target text.\n", encoding="utf-8")
    assert invoke("import", str(source), "-d", str(database)).exit_code == 0
    historical_at = datetime.now(UTC)

    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    library = KnowledgeLibrary(create_session_factory(engine))
    old = library.list_documents()[0]
    source.write_text("# Guide\ncurrent replacement text.\n", encoding="utf-8")
    assert invoke("import", str(source), "-d", str(database)).exit_code == 0
    dataset = EvaluationSet(
        id=uuid4(),
        name="historical vectors",
        version=1,
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
        items=(
            EvaluationItem(
                id="old-version",
                query="historical target",
                expected_lineage_ids=frozenset({old.units[0].lineage_id}),
                at=historical_at,
                source_id=old.source.id,
            ),
        ),
    )
    path = tmp_path / "historical.json"
    write_dataset(path, dataset)
    assert invoke("evaluation", "import", str(path), "-d", str(database)).exit_code == 0
    before = table_rows(database)

    calls = 0
    original = cli._build_demo_index

    def counted(current: KnowledgeLibrary) -> Any:
        nonlocal calls
        calls += 1
        return original(current)

    monkeypatch.setattr(cli, "_build_demo_index", counted)
    output = invoke(
        "evaluation",
        "run",
        str(dataset.id),
        "--mode",
        "vector",
        "--demo-vectors",
        "--json",
        "-d",
        str(database),
    )
    engine.dispose()

    assert output.exit_code == 0, output.stderr
    report = EvaluationReport.model_validate_json(output.stdout)
    assert calls == 1
    assert report.metrics.mean_recall == 1.0
    assert not report.degraded and report.missing_vector_ids == ()
    assert "Warning" in output.stderr and "not semantic" in output.stderr
    assert table_rows(database) == before


def test_configured_sdk_index_reports_each_case_missing_vectors(
    corpus: tuple[Path, KnowledgeLibrary, UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, lineage_id, source_id = corpus
    dataset = make_dataset(lineage_id, source_id)
    path = tmp_path / "configured-missing.json"
    write_dataset(path, dataset)
    assert invoke("evaluation", "import", str(path), "-d", str(database)).exit_code == 0

    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    provider = HashEmbeddingProvider()
    indexing = IndexingService(
        embedding_service=EmbeddingService(provider),
        vector_index=InMemoryVectorIndex(dimension=provider.dimension),
    )
    configured = KnowledgeLibrary(create_session_factory(engine), indexing_service=indexing)

    def open_configured(requested: Path) -> KnowledgeLibrary:
        assert requested == database
        return configured

    monkeypatch.setattr(cli, "_open_library", open_configured)
    before = table_rows(database)
    output = invoke(
        "evaluation",
        "run",
        str(dataset.id),
        "--mode",
        "vector",
        "--json",
        "-d",
        str(database),
    )
    engine.dispose()

    assert output.exit_code == 0, output.stderr
    report = EvaluationReport.model_validate_json(output.stdout)
    assert report.degraded and len(report.missing_vector_ids) == 1
    assert output.stderr.count("eligible unit(s) lack usable vectors") == 2
    assert str(dataset.id) in output.stderr
    assert "item answerable" in output.stderr and "item no-answer" in output.stderr
    assert table_rows(database) == before
