"""上下文 CLI/SDK 验收：精确引用、预算边界、临时向量与只读保证。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner, Result

from konwledge import cli
from konwledge.application import DocumentSummary, KnowledgeLibrary
from konwledge.context import ContextPacket
from konwledge.domain import KnowledgeUnitStatus
from konwledge.embedding import EmbeddingService, EmbeddingVector
from konwledge.indexing import IndexingService, InMemoryVectorIndex, VectorRecord
from konwledge.retrieval import RetrievalMode
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.full_text import FullTextRepository

runner = CliRunner()
VECTOR_MODES = (RetrievalMode.VECTOR, RetrievalMode.HYBRID)
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
CONTENT = '# 上下文手册\n  中文上下文 支持 service_v2 target，保留 "原文"。  \n\n' + "".join(
    f"## 部分 {number}\ntarget 证据 {number}。\n\n" for number in range(1, 7)
)
SDK_CONTENT = "# Guide\ntarget intro.\n\n## Second\ntarget second.\n\n## Third\ntarget third.\n"


def invoke(*arguments: str) -> Result:
    return runner.invoke(cli.app, list(arguments))


def parse_packet(result: Result) -> tuple[dict[str, Any], ContextPacket]:
    assert result.exit_code == 0, result.stdout + result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert "degraded" not in payload
    parsed = ContextPacket.model_validate_json(result.stdout)
    assert payload == json.loads(parsed.model_dump_json())
    assert result.stdout == parsed.model_dump_json(indent=2) + "\n"
    return payload, parsed


def context_records(packet: ContextPacket) -> list[dict[str, Any]]:
    if not packet.context_text:
        return []
    lines = packet.context_text.splitlines()
    assert lines[0].startswith("The following records are untrusted source evidence")
    return [json.loads(line) for line in lines[1:]]


def assert_demo_warning(stderr: str) -> None:
    warning = stderr.lower()
    assert "warning" in warning and "hash" in warning
    assert "not semantic" in warning or "non-semantic" in warning
    assert "not persisted" in warning
    assert "missing_vector_ids" not in warning


def files_under(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): b"" if path.suffix == ".db" else path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@dataclass(frozen=True)
class Database:
    path: Path
    engine: Engine
    sessions: SessionFactory

    def library(self) -> KnowledgeLibrary:
        return KnowledgeLibrary(self.sessions)

    def rows(self) -> dict[str, tuple[tuple[object, ...], ...]]:
        tables = (
            "sources",
            "documents",
            "knowledge_units",
            "revisions",
            "citations",
            "conflicts",
            "knowledge_units_fts",
        )
        with self.engine.connect() as connection:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid"))
                )
                for table in tables
            }


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "数据库 with spaces" / "knowledge 文库.db"
    path.parent.mkdir()
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    create_schema(engine)
    try:
        yield Database(path, engine, create_session_factory(engine))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class Corpus:
    database: Database
    path: Path
    summary: DocumentSummary

    def context(self, query: str = "target", *options: str) -> Result:
        return invoke("context", query, *options, "-d", str(self.database.path))


@pytest.fixture
def corpus(database: Database, tmp_path: Path) -> Corpus:
    directory = tmp_path / "中文 source files"
    directory.mkdir()
    path = directory / "context evidence 含空格.md"
    path.write_text(CONTENT, encoding="utf-8")
    imported = invoke("import", str(path), "-d", str(database.path))
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert "1 document(s) and 7 unit(s) created" in imported.stdout
    [summary] = database.library().list_documents()
    assert len(summary.units) == 7
    return Corpus(database, path, summary)


class ControlledProvider:
    name = "controlled-context"
    model = "query-only-v1"
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        assert all(value == "target" for value in texts), "SDK 不得自动为语料生成 embedding"
        self.calls.append(tuple(texts))
        return [(1.0, 0.0) for _ in texts]


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.now


@dataclass(frozen=True)
class ConfiguredLibrary:
    library: KnowledgeLibrary
    provider: ControlledProvider
    index: InMemoryVectorIndex
    indexing: IndexingService
    clock: Clock
    summary: DocumentSummary


@pytest.fixture
def configured(database: Database, tmp_path: Path) -> ConfiguredLibrary:
    provider = ControlledProvider()
    index = InMemoryVectorIndex(dimension=2)
    indexing = IndexingService(embedding_service=EmbeddingService(provider), vector_index=index)
    clock = Clock()
    library = KnowledgeLibrary(database.sessions, indexing_service=indexing, clock=clock)
    path = tmp_path / "SDK context evidence.md"
    path.write_text(SDK_CONTENT, encoding="utf-8")
    library.import_path(path)
    [summary] = library.list_documents()
    assert len(summary.units) == 3
    assert provider.calls == [] and index.snapshot() == []
    return ConfiguredLibrary(library, provider, index, indexing, clock, summary)


def test_context_json_preserves_exact_quotes_offsets_and_citation_map(corpus: Corpus) -> None:
    before_rows = corpus.database.rows()

    payload, packet = parse_packet(corpus.context("中文上下文", "--json"))

    assert packet.mode is RetrievalMode.LEXICAL
    assert packet.max_chars == 8000 and packet.max_units == 10
    assert packet.include_stale is False
    assert packet.missing_vector_ids == () and not packet.degraded
    assert payload["missing_vector_ids"] == []
    assert packet.skipped == () and len(packet.blocks) == 1

    unit = corpus.summary.units[0]
    block = packet.blocks[0]
    assert block.label == "[1]"
    assert block.status is KnowledgeUnitStatus.ACTIVE
    assert block.citation.quote == unit.text == CONTENT[unit.start_char : unit.end_char]
    assert block.citation.start_char == unit.start_char
    assert block.citation.end_char == unit.end_char
    assert block.citation.knowledge_unit_id == unit.id
    assert block.lineage_id == unit.lineage_id
    assert block.citation.source_id == corpus.summary.source.id
    assert block.citation.document_id == corpus.summary.document.id
    assert block.source_uri == str(corpus.path.resolve())
    assert block.citation.locator == str(corpus.path.resolve())
    assert block.document_checksum == corpus.summary.document.checksum
    assert block.citation.metadata["document_checksum"] == corpus.summary.document.checksum
    assert packet.citations == (block.citation,)
    assert packet.citation_map == {"[1]": block.citation}

    [record] = context_records(packet)
    assert record["label"] == "[1]"
    assert record["citation_id"] == str(block.citation.id)
    assert record["source_uri"] == str(corpus.path.resolve())
    assert record["locator"] == str(corpus.path.resolve())
    assert record["source_id"] == str(corpus.summary.source.id)
    assert record["document_id"] == str(corpus.summary.document.id)
    assert record["unit_id"] == str(unit.id)
    assert record["lineage_id"] == str(unit.lineage_id)
    assert record["document_checksum"] == corpus.summary.document.checksum
    assert record["start_char"] == unit.start_char
    assert record["end_char"] == unit.end_char
    assert record["quote"] == unit.text

    with UnitOfWork(corpus.database.sessions) as uow:
        raw = FullTextRepository(uow.session).search(
            "中文上下文", eligible_ids={item.id for item in corpus.summary.units}, limit=5
        )
    assert raw and raw[0].unit_id == unit.id
    assert corpus.database.rows() == before_rows


def test_context_human_output_contains_prefix_rendered_context_and_skipped_reasons(
    corpus: Corpus,
) -> None:
    expected = corpus.database.library().build_context(
        "target", top_k=3, candidate_limit=3, max_units=2
    )

    result = corpus.context("target", "--top-k", "3", "--candidate-limit", "3", "--max-units", "2")

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.startswith("Context: mode=lexical")
    assert f"evidence={len(expected.blocks)}" in result.stdout
    assert f"skipped={len(expected.skipped)}" in result.stdout
    assert f"used_chars={expected.used_chars}/{expected.max_chars}" in result.stdout
    assert expected.context_text in result.stdout
    for skipped in expected.skipped:
        assert f"unit={skipped.unit_id}" in result.stdout
        assert f"reason={skipped.reason.value}" in result.stdout
    assert "lexical_rank" not in result.stdout
    assert "vector_rank" not in result.stdout
    assert "missing_vector_ids" not in result.stdout


def test_context_top_k_candidate_limit_rrf_and_max_units_are_forwarded(corpus: Corpus) -> None:
    payload, packet = parse_packet(
        corpus.context(
            "target",
            "--top-k",
            "3",
            "--candidate-limit",
            "6",
            "--rrf-k",
            "11",
            "--max-units",
            "2",
            "--json",
        )
    )
    expected = corpus.database.library().build_context(
        "target",
        top_k=3,
        candidate_limit=6,
        rrf_k=11,
        at=packet.evaluated_at,
        max_units=2,
    )

    assert packet == expected
    assert payload["max_units"] == 2 and payload["max_chars"] == 8000
    assert len(packet.blocks) == 2 and len(packet.skipped) == 1
    assert [block.label for block in packet.blocks] == ["[1]", "[2]"]
    assert [item.reason.value for item in packet.skipped] == ["max_units"]


def test_context_source_filter_applies_before_candidate_limit(
    corpus: Corpus, tmp_path: Path
) -> None:
    other = tmp_path / "其他 source 资料.txt"
    other.write_text("target target target", encoding="utf-8")
    imported = invoke("import", str(other), "-d", str(corpus.database.path))
    assert imported.exit_code == 0, imported.stdout + imported.stderr

    summaries = corpus.database.library().list_documents()
    for summary in summaries:
        payload, packet = parse_packet(
            corpus.context(
                "target",
                "--source",
                str(summary.source.id),
                "--top-k",
                "1",
                "--candidate-limit",
                "1",
                "--json",
            )
        )
        assert payload["blocks"]
        assert len(packet.blocks) == 1
        [block] = packet.blocks
        assert block.citation.source_id == summary.source.id
        assert block.citation.knowledge_unit_id in {unit.id for unit in summary.units}
        assert block.source_uri == summary.source.uri
        assert block.citation.locator == summary.document.locator


def test_context_zero_budget_returns_empty_packet_and_keeps_database_unchanged(
    corpus: Corpus,
) -> None:
    before_rows = corpus.database.rows()

    payload, packet = parse_packet(corpus.context("中文上下文", "--max-chars", "0", "--json"))

    assert payload["context_text"] == ""
    assert packet.blocks == ()
    assert [item.reason.value for item in packet.skipped] == ["budget"]
    assert packet.used_chars == 0 and packet.context_text == ""
    assert packet.missing_vector_ids == () and not packet.degraded
    assert corpus.database.rows() == before_rows


def test_context_stale_default_exclusion_and_explicit_inclusion(corpus: Corpus) -> None:
    unit = corpus.summary.units[0]
    marked = invoke(
        "stale", str(unit.id), "--reason", "等待人工复核", "-d", str(corpus.database.path)
    )
    assert marked.exit_code == 0, marked.stdout + marked.stderr

    _, excluded = parse_packet(corpus.context("中文上下文", "--json"))
    assert excluded.blocks == () and excluded.skipped == ()

    _, included = parse_packet(corpus.context("中文上下文", "--include-stale", "--json"))
    assert included.include_stale and len(included.blocks) == 1
    [block] = included.blocks
    assert block.status is KnowledgeUnitStatus.STALE
    assert block.stale_reason == "等待人工复核"
    assert block.citation.knowledge_unit_id == unit.id


def test_context_historical_asof_uses_authoritative_status_not_current_unit_cache(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "version history.md"
    original = "# Policy\nretiredterm 原来的规则。\n"
    path.write_text(original, encoding="utf-8")
    assert invoke("import", str(path), "-d", str(database.path)).exit_code == 0
    historical_at = datetime.now(UTC)
    old = database.library().list_documents()[0].units[0]

    path.write_text("# Policy\nmodernterm 新规则。\n", encoding="utf-8")
    assert invoke("import", str(path), "-d", str(database.path)).exit_code == 0

    _, current = parse_packet(invoke("context", "retiredterm", "--json", "-d", str(database.path)))
    assert current.blocks == ()
    _, also_current = parse_packet(
        invoke("context", "retiredterm", "--include-stale", "--json", "-d", str(database.path))
    )
    assert also_current.blocks == ()

    _, historical = parse_packet(
        invoke(
            "context",
            "retiredterm",
            "--at",
            historical_at.isoformat(),
            "--json",
            "-d",
            str(database.path),
        )
    )
    assert historical.evaluated_at == historical_at and len(historical.blocks) == 1
    [block] = historical.blocks
    assert block.citation.knowledge_unit_id == old.id
    assert block.citation.quote == original
    assert block.status != block.citation.metadata.get("status")
    assert block.status is KnowledgeUnitStatus.ACTIVE
    assert database.library().show_unit(old.id).unit.status is KnowledgeUnitStatus.SUPERSEDED


@pytest.mark.parametrize("mode", VECTOR_MODES)
def test_demo_vectors_context_are_deterministic_and_do_not_persist(
    corpus: Corpus, tmp_path: Path, mode: RetrievalMode
) -> None:
    before_rows = corpus.database.rows()
    before_files = files_under(tmp_path)
    results = [
        corpus.context(
            "target",
            "--mode",
            mode.value,
            "--demo-vectors",
            "--top-k",
            "4",
            "--candidate-limit",
            "6",
            "--rrf-k",
            "13",
            "--json",
        )
        for _ in range(2)
    ]

    normalized: list[dict[str, Any]] = []
    for output in results:
        payload, packet = parse_packet(output)
        assert_demo_warning(output.stderr)
        assert packet.mode is mode and len(packet.blocks) == 4
        assert packet.max_chars == 8000 and packet.max_units == 10
        assert packet.missing_vector_ids == () and not packet.degraded
        for block in packet.blocks:
            assert block.citation.knowledge_unit_id in {unit.id for unit in corpus.summary.units}
            assert block.status is KnowledgeUnitStatus.ACTIVE
            assert (
                block.citation.quote == CONTENT[block.citation.start_char : block.citation.end_char]
            )
            assert block.citation.source_id == corpus.summary.source.id
            assert block.citation.document_id == corpus.summary.document.id
            ranks = [rank for rank in (block.lexical_rank, block.vector_rank) if rank is not None]
            assert ranks
            if mode is RetrievalMode.VECTOR:
                assert block.lexical_rank is None
                assert block.vector_rank is not None
            else:
                assert block.lexical_rank is not None or block.vector_rank is not None
        payload.pop("evaluated_at")
        normalized.append(payload)
    assert normalized[0] == normalized[1]
    assert results[0].stderr == results[1].stderr
    assert corpus.database.rows() == before_rows
    assert files_under(tmp_path) == before_files

    without_demo = corpus.context("target", "--mode", mode.value, "--json")
    assert without_demo.exit_code == 2
    assert without_demo.stdout == ""
    assert "indexing_service" in without_demo.stderr


@pytest.mark.parametrize("mode", tuple(RetrievalMode))
def test_sdk_build_context_uses_facade_shared_service_single_clock_and_query_only_provider(
    configured: ConfiguredLibrary, database: Database, mode: RetrievalMode
) -> None:
    setup = configured
    vectors = ((0.6, 0.8), (1.0, 0.0), (0.0, 1.0))
    setup.index.upsert(
        [
            VectorRecord(
                id=str(unit.id), vector=vector, payload={"content_hash": unit.content_hash}
            )
            for unit, vector in zip(setup.summary.units, vectors, strict=True)
        ]
    )
    before_rows = database.rows()
    before_vectors = setup.index.snapshot()
    setup.clock.now += timedelta(days=1)
    setup.clock.calls = 0

    packet = setup.library.build_context(
        "target",
        mode=mode,
        top_k=2,
        candidate_limit=2,
        rrf_k=7,
        include_stale=True,
        source_id=setup.summary.source.id,
        max_chars=12000,
        max_units=2,
    )
    assert setup.library.context.retrieval is setup.library.retrieval
    assert packet.evaluated_at == setup.clock.now and setup.clock.calls == 1

    through_service = setup.library.context.build(
        "target",
        mode=mode,
        top_k=2,
        candidate_limit=2,
        rrf_k=7,
        at=setup.clock.now,
        include_stale=True,
        source_id=setup.summary.source.id,
        max_chars=12000,
        max_units=2,
    )
    assert setup.clock.calls == 1
    assert through_service == packet
    assert len(packet.blocks) == 2
    assert packet.missing_vector_ids == () and not packet.degraded
    if mode is RetrievalMode.VECTOR:
        assert [block.citation.knowledge_unit_id for block in packet.blocks] == [
            setup.summary.units[1].id,
            setup.summary.units[0].id,
        ]
        assert [block.vector_rank for block in packet.blocks] == [1, 2]
        assert all(block.lexical_rank is None for block in packet.blocks)
    elif mode is RetrievalMode.HYBRID:
        assert any(block.vector_rank is not None for block in packet.blocks)
        assert any(block.lexical_rank is not None for block in packet.blocks)
    for block in packet.blocks:
        assert block.status is KnowledgeUnitStatus.ACTIVE
        assert (
            block.citation.quote == SDK_CONTENT[block.citation.start_char : block.citation.end_char]
        )
    assert setup.provider.calls == ([] if mode is RetrievalMode.LEXICAL else [("target",)] * 2)
    assert setup.index.snapshot() == before_vectors
    assert database.rows() == before_rows


@pytest.mark.parametrize("mode", VECTOR_MODES)
@pytest.mark.parametrize("partial", [False, True])
def test_missing_usable_vectors_surface_in_sdk_and_cli_without_auto_repair(
    configured: ConfiguredLibrary,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    mode: RetrievalMode,
    partial: bool,
) -> None:
    setup = configured
    if partial:
        valid, _, mismatched = setup.summary.units
        setup.index.upsert(
            [
                VectorRecord(
                    id=str(valid.id),
                    vector=(1.0, 0.0),
                    payload={"content_hash": valid.content_hash, "text": "forged"},
                ),
                VectorRecord(
                    id=str(mismatched.id),
                    vector=(1.0, 0.0),
                    payload={"content_hash": "obsolete hash"},
                ),
            ]
        )
    expected_missing = tuple(sorted(unit.id for unit in setup.summary.units[int(partial) :]))
    before_rows = database.rows()
    before_vectors = setup.index.snapshot()

    setup.clock.calls = 0
    setup.provider.calls.clear()
    sdk_packet = setup.library.build_context(
        "target",
        mode=mode,
        top_k=3,
        candidate_limit=3,
        max_chars=12000,
        max_units=3,
    )
    assert sdk_packet.degraded and sdk_packet.missing_vector_ids == expected_missing
    assert sdk_packet.evaluated_at == setup.clock.now and setup.clock.calls == 1
    if mode is RetrievalMode.VECTOR:
        assert {block.citation.knowledge_unit_id for block in sdk_packet.blocks} == (
            {setup.summary.units[0].id} if partial else set()
        )
    else:
        assert {block.citation.knowledge_unit_id for block in sdk_packet.blocks} == {
            unit.id for unit in setup.summary.units
        }
        missing_blocks = [
            block
            for block in sdk_packet.blocks
            if block.citation.knowledge_unit_id in expected_missing
        ]
        assert all(block.vector_rank is None for block in missing_blocks)
        assert all(block.lexical_rank is not None for block in missing_blocks)
    assert all(block.citation.quote != "forged" for block in sdk_packet.blocks)
    assert setup.provider.calls == [("target",)]

    def open_configured(path: Path) -> KnowledgeLibrary:
        assert path == database.path
        return setup.library

    monkeypatch.setattr(cli, "_open_library", open_configured)
    setup.provider.calls.clear()
    output = invoke(
        "context",
        "target",
        "--mode",
        mode.value,
        "--max-chars",
        "12000",
        "--max-units",
        "3",
        "--json",
        "-d",
        str(database.path),
    )
    payload, packet = parse_packet(output)
    assert packet.degraded and packet.missing_vector_ids == expected_missing
    assert payload["missing_vector_ids"] == [str(unit_id) for unit_id in expected_missing]
    assert f"{len(expected_missing)} eligible unit(s) lack usable vectors" in output.stderr
    assert "vector recall is incomplete" in output.stderr and "missing_vector_ids=" in output.stderr
    for unit_id in expected_missing:
        assert str(unit_id) in output.stderr
    if mode is RetrievalMode.VECTOR:
        assert {block.citation.knowledge_unit_id for block in packet.blocks} == (
            {setup.summary.units[0].id} if partial else set()
        )
    else:
        assert {block.citation.knowledge_unit_id for block in packet.blocks} == {
            unit.id for unit in setup.summary.units
        }
        missing_blocks = [
            block for block in packet.blocks if block.citation.knowledge_unit_id in expected_missing
        ]
        assert all(block.vector_rank is None for block in missing_blocks)
        assert all(block.lexical_rank is not None for block in missing_blocks)
    assert all(block.citation.quote != "forged" for block in packet.blocks)
    assert setup.provider.calls == [("target",)]
    assert setup.index.snapshot() == before_vectors
    assert database.rows() == before_rows


@pytest.mark.parametrize(
    "arguments,diagnostic",
    [
        (("context",), "Missing argument"),
        (("context", ""), "query"),
        (("context", " \t\n"), "query"),
        (("context", "target", "--top-k", "0"), "--top-k"),
        (("context", "target", "--candidate-limit", "0"), "--candidate-limit"),
        (("context", "target", "--top-k", "5", "--candidate-limit", "4"), "candidate_limit"),
        (("context", "target", "--rrf-k", "0"), "--rrf-k"),
        (("context", "target", "--mode", "semantic"), "--mode"),
        (("context", "target", "--source", "not-a-uuid"), "--source"),
        (("context", "target", "--at", "not-a-date"), "--at"),
        (("context", "target", "--demo-vectors"), "--demo-vectors"),
        (("context", "target", "--mode", "lexical", "--demo-vectors"), "--demo-vectors"),
        (("context", "target", "--max-chars", "-1"), "--max-chars"),
        (("context", "target", "--max-units", "0"), "--max-units"),
    ],
)
def test_invalid_context_options_exit_two_without_tracebacks(
    database: Database, arguments: tuple[str, ...], diagnostic: str
) -> None:
    result = invoke(*arguments, "--json", "-d", str(database.path))
    assert result.exit_code == 2, result.stdout + result.stderr
    assert diagnostic in (result.stdout + result.stderr)
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "option,value",
    [("--max-chars", "-1"), ("--max-units", "0")],
)
def test_invalid_budget_fails_before_opening_or_creating_database(
    tmp_path: Path, option: str, value: str
) -> None:
    database = tmp_path / "missing dir" / "knowledge.db"

    result = invoke("context", "target", option, value, "--json", "-d", str(database))

    assert result.exit_code == 2
    assert option in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert not database.exists()
    assert not database.parent.exists()


def test_empty_database_context_is_success(database: Database) -> None:
    payload, packet = parse_packet(invoke("context", "target", "--json", "-d", str(database.path)))
    assert payload["blocks"] == [] and payload["skipped"] == []
    assert packet.blocks == () and packet.skipped == ()
    assert packet.context_text == "" and packet.used_chars == 0
    assert packet.missing_vector_ids == () and not packet.degraded


@pytest.mark.parametrize(
    "options",
    [
        ("--mode", "vector"),
        ("--max-chars", "-1"),
        ("--at", "bad-date"),
    ],
)
def test_main_returns_usage_error_code_and_keeps_errors_on_stderr(
    database: Database, capsys: pytest.CaptureFixture[str], options: tuple[str, ...]
) -> None:
    assert cli.main(["context", "target", *options, "--json", "-d", str(database.path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("mode", tuple(RetrievalMode))
def test_python_module_cli_context_works_with_space_paths_and_separate_streams(
    corpus: Corpus, mode: RetrievalMode
) -> None:
    options = ["--mode", mode.value]
    if mode is not RetrievalMode.LEXICAL:
        options.append("--demo-vectors")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "konwledge",
            "context",
            "target",
            *options,
            "--json",
            "-d",
            str(corpus.database.path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    packet = ContextPacket.model_validate_json(completed.stdout)
    assert "degraded" not in json.loads(completed.stdout)
    assert packet.blocks and packet.mode is mode
    assert packet.missing_vector_ids == ()
    assert all(block.source_uri == str(corpus.path.resolve()) for block in packet.blocks)
    if mode is RetrievalMode.LEXICAL:
        assert completed.stderr == ""
    else:
        assert_demo_warning(completed.stderr)


def test_context_read_only_calls_do_not_create_citations_revisions_or_fts_rows(
    corpus: Corpus,
) -> None:
    before_rows = corpus.database.rows()

    lexical = corpus.context("中文上下文", "--json")
    assert lexical.exit_code == 0, lexical.stdout + lexical.stderr
    vector = corpus.context("target", "--mode", "hybrid", "--demo-vectors", "--json")
    assert vector.exit_code == 0, vector.stdout + vector.stderr

    after_rows = corpus.database.rows()
    assert after_rows["citations"] == before_rows["citations"] == ()
    assert after_rows["revisions"] == before_rows["revisions"]
    assert after_rows["knowledge_units_fts"] == before_rows["knowledge_units_fts"]
    assert after_rows == before_rows
