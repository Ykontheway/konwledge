"""检索的 CLI/SDK 验收：真实文件数据库、证据溯源、历史状态与临时向量。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner, Result

from konwledge import cli
from konwledge.application import DocumentSummary, KnowledgeLibrary
from konwledge.domain import KnowledgeUnitStatus
from konwledge.embedding import EmbeddingService, EmbeddingVector
from konwledge.indexing import IndexingService, InMemoryVectorIndex, VectorRecord
from konwledge.retrieval import RetrievalConfigurationError, RetrievalMode, RetrievalResult
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
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
CONTENT = '# 检索手册\n  中文检索 支持 service_v2 target，保留 "原文"。  \n\n' + "".join(
    f"## Section {number}\ntarget evidence {number}.\n\n" for number in range(1, 7)
)
SDK_CONTENT = "# Guide\ntarget intro.\n\n## Second\ntarget second.\n\n## Third\ntarget third.\n"


def invoke(*arguments: str) -> Result:
    return runner.invoke(cli.app, list(arguments))


def parse_result(result: Result) -> tuple[dict[str, Any], RetrievalResult]:
    assert result.exit_code == 0, result.stdout + result.stderr
    # json.loads 消费整个 stdout；任何混入的警告/标题都会导致失败。
    payload: dict[str, Any] = json.loads(result.stdout)
    assert "degraded" not in payload  # 仅 SDK 属性，不是 computed_field。
    parsed = RetrievalResult.model_validate_json(result.stdout)
    assert payload == json.loads(parsed.model_dump_json())
    assert result.stdout == parsed.model_dump_json(indent=2) + "\n"
    return payload, parsed


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
    path = tmp_path / "database with spaces" / "knowledge library.db"
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

    def search(self, query: str = "target", *options: str) -> Result:
        return invoke("search", query, *options, "-d", str(self.database.path))


@pytest.fixture
def corpus(database: Database, tmp_path: Path) -> Corpus:
    directory = tmp_path / "中文 source files"
    directory.mkdir()
    path = directory / "retrieval evidence with spaces.md"
    path.write_text(CONTENT, encoding="utf-8")
    imported = invoke("import", str(path), "-d", str(database.path))
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert "1 document(s) and 7 unit(s) created" in imported.stdout
    summary = database.library().list_documents()[0]
    assert len(summary.units) == 7
    return Corpus(database, path, summary)


@pytest.mark.parametrize("query", ["中文检索", "service_v2", "SERVICE_V2"])
def test_lexical_json_preserves_complete_original_evidence(corpus: Corpus, query: str) -> None:
    payload, result = parse_result(corpus.search(query, "--json"))
    assert result.mode is RetrievalMode.LEXICAL
    assert result.top_k == 5 and result.candidate_limit == 50 and result.rrf_k == 60
    assert not result.include_stale and not result.degraded
    assert payload["missing_vector_ids"] == []
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.unit == corpus.summary.units[0]
    assert hit.document == corpus.summary.document
    assert hit.source == corpus.summary.source
    assert hit.unit.text == CONTENT[hit.unit.start_char : hit.unit.end_char]
    assert '  中文检索 支持 service_v2 target，保留 "原文"。  \n' in hit.unit.text
    assert hit.unit.lineage_id == hit.unit.id
    assert hit.unit.document_id == hit.document.id
    assert hit.document.source_id == hit.source.id
    assert hit.source.uri == hit.document.locator == str(corpus.path.resolve())
    assert hit.source.title == corpus.path.name
    assert hit.source.kind.value == "file"
    assert hit.unit.section_path == ("检索手册",) and hit.unit.heading == "检索手册"
    assert hit.status is KnowledgeUnitStatus.ACTIVE
    assert hit.lexical_rank == 1 and hit.vector_rank is None and hit.vector_score is None
    assert hit.stale_reason is None
    assert hit.score == pytest.approx(1 / 61)
    with UnitOfWork(corpus.database.sessions) as uow:
        raw = FullTextRepository(uow.session).search(
            query, eligible_ids={unit.id for unit in corpus.summary.units}, limit=5
        )
    assert hit.lexical_score == raw[0].score < 0
    assert set(payload["hits"][0]) == {
        "unit",
        "document",
        "source",
        "status",
        "stale_reason",
        "score",
        "lexical_rank",
        "vector_rank",
        "lexical_score",
        "vector_score",
    }


def test_lexical_human_output_contains_source_offsets_lineage_and_unabridged_text(
    corpus: Corpus,
) -> None:
    result = corpus.search("中文检索")
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    unit = corpus.summary.units[0]
    assert "Search: mode=lexical hits=1" in result.stdout
    assert "degraded=false" in result.stdout
    for evidence in (
        unit.text,
        f"unit={unit.id}",
        "status=active",
        "lexical_rank=1 vector_rank=None",
        f"source={corpus.path.resolve()}",
        f"document={unit.document_id}",
        f"lineage={unit.lineage_id}",
        f"range=[{unit.start_char}, {unit.end_char})",
        "section=检索手册",
    ):
        assert evidence in result.stdout


@pytest.mark.parametrize("top_k,candidate_limit", [(1, 1), (2, 2), (3, 6), (10, 10)])
def test_top_k_and_candidate_limit_are_forwarded_and_enforced(
    corpus: Corpus, top_k: int, candidate_limit: int
) -> None:
    _, default = parse_result(corpus.search("target", "--json"))
    assert len(default.hits) == 5
    _, result = parse_result(
        corpus.search(
            "target",
            "--top-k",
            str(top_k),
            "--candidate-limit",
            str(candidate_limit),
            "--rrf-k",
            "10",
            "--json",
        )
    )
    assert result.top_k == top_k and result.candidate_limit == candidate_limit
    assert result.rrf_k == 10
    assert len(result.hits) == min(top_k, 7)
    assert len({hit.unit.id for hit in result.hits}) == len(result.hits)
    for rank, hit in enumerate(result.hits, start=1):
        assert hit.lexical_rank == rank
        assert hit.score == pytest.approx(1 / (10 + rank))
        assert hit.unit.text == CONTENT[hit.unit.start_char : hit.unit.end_char]


def test_source_filter_is_applied_before_candidate_limit(corpus: Corpus, tmp_path: Path) -> None:
    other = tmp_path / "other source.txt"
    other.write_text("target target target", encoding="utf-8")
    assert invoke("import", str(other), "-d", str(corpus.database.path)).exit_code == 0
    summaries = corpus.database.library().list_documents()
    other_summary = next(summary for summary in summaries if summary.source.uri == str(other))
    for summary in (corpus.summary, other_summary):
        _, result = parse_result(
            corpus.search(
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
        assert len(result.hits) == 1
        assert result.hits[0].source == summary.source
        assert result.hits[0].unit.id in {unit.id for unit in summary.units}


def test_stale_default_exclusion_and_explicit_inclusion(corpus: Corpus) -> None:
    unit = corpus.summary.units[0]
    marked = invoke(
        "stale", str(unit.id), "--reason", "等待人工复核", "-d", str(corpus.database.path)
    )
    assert marked.exit_code == 0, marked.stderr
    _, excluded = parse_result(corpus.search("中文检索", "--json"))
    assert excluded.hits == ()
    _, included = parse_result(corpus.search("中文检索", "--include-stale", "--json"))
    assert included.include_stale and len(included.hits) == 1
    # 命中层 status 来自 revision 解析；不依赖 unit.status 缓存决定有效性。
    assert included.hits[0].status is KnowledgeUnitStatus.STALE
    assert included.hits[0].stale_reason == "等待人工复核"


def test_old_term_excluded_now_but_asof_returns_authoritative_active_status(
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
    _, current = parse_result(invoke("search", "retiredterm", "--json", "-d", str(database.path)))
    assert current.hits == ()
    _, also_current = parse_result(
        invoke("search", "retiredterm", "--include-stale", "--json", "-d", str(database.path))
    )
    assert also_current.hits == ()
    _, historical = parse_result(
        invoke(
            "search",
            "retiredterm",
            "--at",
            historical_at.isoformat(),
            "--json",
            "-d",
            str(database.path),
        )
    )
    assert historical.evaluated_at == historical_at and len(historical.hits) == 1
    hit = historical.hits[0]
    assert hit.unit.id == old.id and hit.unit.text == original
    assert hit.status != hit.unit.status
    assert hit.status is KnowledgeUnitStatus.ACTIVE
    assert hit.unit.status is KnowledgeUnitStatus.SUPERSEDED
    assert database.library().show_unit(old.id).unit.status is KnowledgeUnitStatus.SUPERSEDED


def assert_demo_warning(stderr: str) -> None:
    warning = stderr.lower()
    assert "warning" in warning and "hash" in warning
    assert "not semantic" in warning or "non-semantic" in warning
    assert "not persisted" in warning
    assert "missing_vector_ids" not in warning


def files_under(root: Path) -> dict[Path, bytes]:
    # 比较新增路径及非 DB 内容；DB 本身用 rows 比较，不把 SQLite 页布局当契约。
    return {
        path.relative_to(root): b"" if path.suffix == ".db" else path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("mode", VECTOR_MODES)
def test_demo_vectors_are_deterministic_json_only_and_do_not_persist(
    corpus: Corpus, tmp_path: Path, mode: RetrievalMode
) -> None:
    before_rows = corpus.database.rows()
    before_files = files_under(tmp_path)
    results = [
        corpus.search(
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
        payload, result = parse_result(output)
        assert_demo_warning(output.stderr)
        assert result.mode is mode and len(result.hits) == 4
        assert result.top_k == 4 and result.candidate_limit == 6 and result.rrf_k == 13
        assert result.missing_vector_ids == () and not result.degraded
        for hit in result.hits:
            assert hit.unit.id in {unit.id for unit in corpus.summary.units}
            assert hit.status is KnowledgeUnitStatus.ACTIVE
            assert hit.unit.text == CONTENT[hit.unit.start_char : hit.unit.end_char]
            assert hit.source == corpus.summary.source and hit.document == corpus.summary.document
            ranks = [rank for rank in (hit.lexical_rank, hit.vector_rank) if rank is not None]
            assert ranks
            assert hit.score == pytest.approx(sum(1 / (13 + rank) for rank in ranks))
            if hit.vector_rank is not None:
                assert hit.vector_score is not None and -1 <= hit.vector_score <= 1
            if mode is RetrievalMode.VECTOR:
                assert hit.lexical_rank is None and hit.lexical_score is None
            else:
                assert hit.lexical_rank is not None or hit.vector_rank is not None
        payload.pop("evaluated_at")
        normalized.append(payload)
    assert normalized[0] == normalized[1]
    assert results[0].stderr == results[1].stderr
    assert corpus.database.rows() == before_rows
    assert files_under(tmp_path) == before_files
    # 再开 CLI，不携带 flag，不能找到上一轮临时索引配置。
    without_demo = corpus.search("target", "--mode", mode.value, "--json")
    assert without_demo.exit_code == 2
    assert "indexing_service" in without_demo.stderr and without_demo.stdout == ""


@pytest.mark.parametrize("mode", VECTOR_MODES)
def test_demo_indexes_all_stored_versions_and_stale_units_without_reconciliation(
    database: Database, tmp_path: Path, mode: RetrievalMode
) -> None:
    path = tmp_path / "historical vectors.md"
    path.write_text(SDK_CONTENT, encoding="utf-8")
    assert invoke("import", str(path), "-d", str(database.path)).exit_code == 0
    first = database.library().list_documents()[0]
    historical_at = datetime.now(UTC)
    path.write_text(SDK_CONTENT.replace("target", "modernterm"), encoding="utf-8")
    assert invoke("import", str(path), "-d", str(database.path)).exit_code == 0
    current = next(summary for summary in database.library().list_documents() if summary.is_head)
    assert (
        invoke(
            "stale", str(current.units[0].id), "--reason", "review", "-d", str(database.path)
        ).exit_code
        == 0
    )
    before_rows, before_files = database.rows(), files_under(tmp_path)
    common = ["--mode", mode.value, "--demo-vectors", "--json", "-d", str(database.path)]
    historical_output = invoke("search", "target", "--at", historical_at.isoformat(), *common)
    _, historical = parse_result(historical_output)
    assert_demo_warning(historical_output.stderr)
    assert {hit.unit.id for hit in historical.hits} == {unit.id for unit in first.units}
    assert historical.missing_vector_ids == ()
    assert all(hit.status is KnowledgeUnitStatus.ACTIVE for hit in historical.hits)
    assert all(hit.unit.status is KnowledgeUnitStatus.SUPERSEDED for hit in historical.hits)
    _, excluded = parse_result(invoke("search", "modernterm", *common))
    assert {hit.unit.id for hit in excluded.hits} == {unit.id for unit in current.units[1:]}
    included_output = invoke("search", "modernterm", "--include-stale", *common)
    _, included = parse_result(included_output)
    assert_demo_warning(included_output.stderr)
    assert {hit.unit.id for hit in included.hits} == {unit.id for unit in current.units}
    stale = next(hit for hit in included.hits if hit.unit.id == current.units[0].id)
    assert stale.status is KnowledgeUnitStatus.STALE and stale.stale_reason == "review"
    assert included.missing_vector_ids == ()
    assert database.rows() == before_rows and files_under(tmp_path) == before_files


@pytest.mark.parametrize("mode", VECTOR_MODES)
@pytest.mark.parametrize("populated", [False, True])
def test_vector_configuration_missing_is_exit_two(
    database: Database, tmp_path: Path, mode: RetrievalMode, populated: bool
) -> None:
    if populated:
        path = tmp_path / "populated.txt"
        path.write_text("target", encoding="utf-8")
        assert invoke("import", str(path), "-d", str(database.path)).exit_code == 0
    result = invoke("search", "target", "--mode", mode.value, "--json", "-d", str(database.path))
    assert result.exit_code == 2
    assert result.stdout == ""
    assert mode.value in result.stderr and "indexing_service" in result.stderr


@pytest.mark.parametrize(
    "arguments,diagnostic",
    [
        (("search",), "Missing argument"),
        (("search", ""), "query"),
        (("search", " \t\n"), "query"),
        (("search", "target", "--top-k", "0"), "--top-k"),
        (("search", "target", "--top-k", "-1"), "--top-k"),
        (("search", "target", "--top-k", "1.5"), "--top-k"),
        (("search", "target", "--candidate-limit", "0"), "--candidate-limit"),
        (("search", "target", "--candidate-limit", "-1"), "--candidate-limit"),
        (("search", "target", "--candidate-limit", "many"), "--candidate-limit"),
        (("search", "target", "--top-k", "5", "--candidate-limit", "4"), "candidate_limit"),
        (("search", "target", "--rrf-k", "0"), "--rrf-k"),
        (("search", "target", "--rrf-k", "-1"), "--rrf-k"),
        (("search", "target", "--rrf-k", "sixty"), "--rrf-k"),
        (("search", "target", "--mode", "semantic"), "--mode"),
        (("search", "target", "--source", "not-a-uuid"), "--source"),
        (("search", "target", "--at", "not-a-date"), "--at"),
        (("search", "target", "--at", "2026-13-40T12:00:00Z"), "--at"),
        (("search", "target", "--demo-vectors"), "--demo-vectors"),
        (("search", "target", "--mode", "lexical", "--demo-vectors"), "--demo-vectors"),
    ],
)
def test_invalid_search_options_exit_two_without_tracebacks(
    database: Database, arguments: tuple[str, ...], diagnostic: str
) -> None:
    result = invoke(*arguments, "--json", "-d", str(database.path))
    assert result.exit_code == 2, result.stdout + result.stderr
    assert diagnostic in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("query", ["!!! ???", "，。？", "absentuniqueterm"])
@pytest.mark.parametrize("json_output", [False, True])
def test_punctuation_and_no_hits_are_successful_not_query_errors(
    corpus: Corpus, query: str, json_output: bool
) -> None:
    output = corpus.search(query, *(["--json"] if json_output else []))
    assert output.exit_code == 0 and output.stderr == ""
    if json_output:
        _, result = parse_result(output)
        assert result.hits == () and result.missing_vector_ids == ()
    else:
        assert "hits=0" in output.stdout and "No matching knowledge found." in output.stdout


@pytest.mark.parametrize("mode", tuple(RetrievalMode))
def test_unknown_source_is_empty_success(corpus: Corpus, mode: RetrievalMode) -> None:
    options = ["--mode", mode.value, "--source", str(uuid4()), "--json"]
    if mode is not RetrievalMode.LEXICAL:
        options.append("--demo-vectors")
    output = corpus.search("target", *options)
    _, result = parse_result(output)
    assert result.hits == () and result.missing_vector_ids == () and not result.degraded
    if mode is not RetrievalMode.LEXICAL:
        assert_demo_warning(output.stderr)
    else:
        assert output.stderr == ""


def test_empty_database_search_is_success(database: Database) -> None:
    output = invoke("search", "target", "--json", "-d", str(database.path))
    _, result = parse_result(output)
    assert result.hits == () and result.missing_vector_ids == () and not result.degraded
    assert output.stderr == ""


class ControlledProvider:
    name = "controlled-cli"
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
    path = tmp_path / "SDK evidence.md"
    path.write_text(SDK_CONTENT, encoding="utf-8")
    library.import_path(path)
    summary = library.list_documents()[0]
    assert len(summary.units) == 3
    assert provider.calls == [] and index.snapshot() == []
    return ConfiguredLibrary(library, provider, index, indexing, clock, summary)


@pytest.mark.parametrize("mode", tuple(RetrievalMode))
def test_sdk_search_and_retrieval_use_injected_clock_index_and_query_only_provider(
    configured: ConfiguredLibrary, database: Database, mode: RetrievalMode
) -> None:
    setup = configured
    assert setup.library.retrieval is setup.library.retrieval
    assert setup.library.retrieval.indexing_service is setup.indexing
    vectors = ((0.6, 0.8), (1.0, 0.0), (0.0, 1.0))
    setup.index.upsert(
        [
            VectorRecord(
                id=str(unit.id), vector=vector, payload={"content_hash": unit.content_hash}
            )
            for unit, vector in zip(setup.summary.units, vectors, strict=True)
        ]
    )
    before_rows, before_vectors = database.rows(), setup.index.snapshot()
    setup.clock.now += timedelta(days=1)
    setup.clock.calls = 0
    through_library = setup.library.search(
        "target",
        mode=mode,
        top_k=2,
        candidate_limit=2,
        rrf_k=7,
        include_stale=True,
        source_id=setup.summary.source.id,
    )
    assert through_library.evaluated_at == setup.clock.now and setup.clock.calls == 1
    through_service = setup.library.retrieval.search(
        "target",
        mode=mode,
        top_k=2,
        candidate_limit=2,
        rrf_k=7,
        at=setup.clock.now,
        include_stale=True,
        source_id=setup.summary.source.id,
    )
    assert setup.clock.calls == 1  # 显式 at 不调用注入 clock。
    assert through_service == through_library
    assert len(through_library.hits) == 2
    assert through_library.include_stale and through_library.rrf_k == 7
    assert through_library.top_k == through_library.candidate_limit == 2
    assert through_library.missing_vector_ids == () and not through_library.degraded
    if mode is RetrievalMode.VECTOR:
        assert [hit.unit.id for hit in through_library.hits] == [
            setup.summary.units[1].id,
            setup.summary.units[0].id,
        ]
        assert [hit.vector_score for hit in through_library.hits] == pytest.approx([1.0, 0.6])
        assert [hit.vector_rank for hit in through_library.hits] == [1, 2]
    elif mode is RetrievalMode.HYBRID:
        assert any(hit.vector_rank is not None for hit in through_library.hits)
        assert any(hit.lexical_rank is not None for hit in through_library.hits)
    for hit in through_library.hits:
        assert hit.status is KnowledgeUnitStatus.ACTIVE
        assert hit.unit.text == SDK_CONTENT[hit.unit.start_char : hit.unit.end_char]
    assert setup.provider.calls == ([] if mode is RetrievalMode.LEXICAL else [("target",)] * 2)
    assert setup.index.snapshot() == before_vectors and database.rows() == before_rows


@pytest.mark.parametrize("mode", VECTOR_MODES)
def test_sdk_unconfigured_library_reports_configuration_error(
    database: Database, mode: RetrievalMode
) -> None:
    library = database.library()
    assert library.retrieval.indexing_service is None
    for search in (library.search, library.retrieval.search):
        with pytest.raises(RetrievalConfigurationError, match="indexing_service"):
            search("target", mode=mode)
    assert library.search("target").hits == ()


@pytest.mark.parametrize("mode", VECTOR_MODES)
@pytest.mark.parametrize("partial", [False, True])
def test_missing_usable_vectors_visible_in_json_and_stderr_without_auto_repair(
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
    before_rows, before_vectors = database.rows(), setup.index.snapshot()

    def open_configured(path: Path) -> KnowledgeLibrary:
        assert path == database.path
        return setup.library

    # 仅配置告警测试注入真实 library；demo 测试始终走真实 _open_library/第二 engine。
    monkeypatch.setattr(cli, "_open_library", open_configured)
    output = invoke("search", "target", "--mode", mode.value, "--json", "-d", str(database.path))
    payload, result = parse_result(output)
    assert result.degraded and result.missing_vector_ids == expected_missing
    assert payload["missing_vector_ids"] == [str(unit_id) for unit_id in expected_missing]
    assert f"{len(expected_missing)} eligible unit(s) lack usable vectors" in output.stderr
    assert "vector recall is incomplete" in output.stderr and "missing_vector_ids=" in output.stderr
    for unit_id in expected_missing:
        assert str(unit_id) in output.stderr
    if mode is RetrievalMode.VECTOR:
        assert {hit.unit.id for hit in result.hits} == (
            {setup.summary.units[0].id} if partial else set()
        )
    else:
        assert {hit.unit.id for hit in result.hits} == {unit.id for unit in setup.summary.units}
        missing_hits = [hit for hit in result.hits if hit.unit.id in expected_missing]
        assert all(hit.vector_rank is None and hit.vector_score is None for hit in missing_hits)
        assert all(hit.lexical_rank is not None for hit in missing_hits)
    assert all(hit.unit.text != "forged" for hit in result.hits)
    assert setup.provider.calls == [("target",)]
    assert setup.index.snapshot() == before_vectors and database.rows() == before_rows


@pytest.mark.parametrize(
    "options",
    [
        ("--mode", "vector"),
        ("--top-k", "0"),
        ("--at", "bad-date"),
    ],
)
def test_main_returns_usage_error_code_and_keeps_errors_on_stderr(
    database: Database, capsys: pytest.CaptureFixture[str], options: tuple[str, ...]
) -> None:
    assert cli.main(["search", "target", *options, "--json", "-d", str(database.path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("mode", tuple(RetrievalMode))
def test_python_module_cli_works_with_space_paths_and_separate_streams(
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
            "search",
            "中文检索",
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
    result = RetrievalResult.model_validate_json(completed.stdout)
    assert "degraded" not in json.loads(completed.stdout)
    assert result.hits and result.mode is mode
    assert result.missing_vector_ids == ()
    assert all(hit.source.uri == str(corpus.path.resolve()) for hit in result.hits)
    if mode is RetrievalMode.LEXICAL:
        assert completed.stderr == "" and len(result.hits) == 1
    else:
        assert_demo_warning(completed.stderr)
