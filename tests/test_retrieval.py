"""U13: real FTS/imports, controlled cosine, as-of eligibility, and read-only RRF."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, event, text

from konwledge.domain import KnowledgeUnit, KnowledgeUnitStatus, Source, SourceKind
from konwledge.embedding import EmbeddingService, EmbeddingVector
from konwledge.indexing import IndexingService, InMemoryVectorIndex, VectorRecord
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionResult,
    TraceableIngestionService,
)
from konwledge.lifecycle import LifecycleService
from konwledge.retrieval import (
    FusedUnit,
    RankedUnit,
    RetrievalConfigurationError,
    RetrievalHit,
    RetrievalMode,
    RetrievalResult,
    RetrievalService,
    fuse_rankings,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.full_text import FullTextRepository

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
V1 = "# Guide\ntarget intro.\n\n## Legacy\ntarget legacy.\n\n## Edit\ntarget old instructions.\n"
V2 = "# Guide\ntarget intro.\n\n## Edit\ntarget new instructions.\n\n## Added\ntarget added.\n"
MODES = tuple(RetrievalMode)


class ControlledProvider:
    name = "controlled"
    model = "cosine-v1"
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.vector: EmbeddingVector = (1.0, 0.0)
        self.error: Exception | None = None

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        self.calls.append(tuple(texts))
        if self.error is not None:
            raise self.error
        return [self.vector for _ in texts]


class GuardIndex(InMemoryVectorIndex):
    """Allow setup writes, then forbid original-index search or mutation."""

    def __init__(self) -> None:
        super().__init__(dimension=2)
        self.sealed = False
        self.get_calls: list[tuple[str, ...]] = []

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        assert not self.sealed, "retrieval must not upsert the original index"
        super().upsert(records)

    def delete(self, ids: Sequence[str]) -> int:
        assert not self.sealed, "retrieval must not delete from the original index"
        return super().delete(ids)

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        assert not self.sealed, "retrieval must not rebuild the original index"
        super().rebuild(records)

    def get(self, ids: Sequence[str]) -> dict[str, VectorRecord]:
        self.get_calls.append(tuple(ids))
        return super().get(ids)

    def search(
        self,
        query: EmbeddingVector,
        *,
        top_k: int = 10,
        filters: Any = None,
        min_score: float | None = None,
    ) -> list[Any]:
        raise AssertionError("retrieval must search a temporary eligible-only exact index")


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


class Corpus:
    def __init__(self, tmp_path: Path, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory
        self.path = tmp_path / "retrieval.md"
        self.tick = 0
        self.source = Source(
            id=uuid4(),
            kind=SourceKind.FILE,
            uri=str(self.path),
            title="Retrieval evidence",
            created_at=NOW,
            metadata={"owner": "metadata"},
        )
        with UnitOfWork(session_factory) as uow:
            uow.sources.add(self.source)
            uow.commit()
        self.ingestion = TraceableIngestionService(
            FileIngestionService(session_factory, clock=self.clock),
            KnowledgeUnitService(session_factory, clock=self.clock),
        )
        self.lifecycle = LifecycleService(session_factory, clock=self.clock)
        self.provider = ControlledProvider()
        self.index = GuardIndex()
        self.indexing = IndexingService(
            embedding_service=EmbeddingService(self.provider),
            vector_index=self.index,
        )
        self.retrieval = RetrievalService(
            session_factory,
            indexing_service=self.indexing,
            clock=self.now,
        )

    def clock(self) -> datetime:
        self.tick += 1
        return self.now()

    def now(self) -> datetime:
        return NOW + timedelta(minutes=self.tick)

    def ingest(self, content: str = V1) -> TraceableIngestionResult:
        self.path.write_text(content, encoding="utf-8")
        return self.ingestion.ingest(self.source.id)

    def store(
        self,
        unit: KnowledgeUnit,
        vector: EmbeddingVector = (1.0, 0.0),
        **payload: object,
    ) -> None:
        self.index.upsert(
            [
                VectorRecord(
                    id=str(unit.id),
                    vector=vector,
                    payload={"content_hash": unit.content_hash, **payload},
                ),
            ]
        )


def ids(result: RetrievalResult) -> set[UUID]:
    return {hit.unit.id for hit in result.hits}


def test_rrf_hand_calculation_deduplication_and_raw_scores() -> None:
    a, b, c = (UUID(int=value) for value in (1, 2, 3))
    lexical = [RankedUnit(a, -123.0), RankedUnit(a, -999.0), RankedUnit(b, -0.001)]
    vector = [RankedUnit(b, 0.2), RankedUnit(b, 1.0), RankedUnit(c, -0.5), RankedUnit(a, 0.0)]
    fused = fuse_rankings(lexical, vector, rrf_k=10)
    assert [hit.unit_id for hit in fused] == [b, a, c]
    assert fused[0] == FusedUnit(b, 1 / 12 + 1 / 11, 2, 1, -0.001, 0.2)
    assert fused[1] == FusedUnit(a, 1 / 11 + 1 / 13, 1, 3, -123.0, 0.0)
    assert fused[2] == FusedUnit(c, 1 / 12, None, 2, None, -0.5)
    rescaled = fuse_rankings(
        [RankedUnit(hit.unit_id, hit.score * 1000) for hit in lexical],
        [RankedUnit(hit.unit_id, hit.score * -1000) for hit in vector],
        rrf_k=10,
    )
    assert [(hit.unit_id, hit.score) for hit in rescaled] == [
        (hit.unit_id, hit.score) for hit in fused
    ]


def test_rrf_ties_use_uuid_and_single_route_uses_rrf() -> None:
    a, b = UUID(int=1), UUID(int=2)
    lexical = [RankedUnit(b, -2.0), RankedUnit(a, -1.0)]
    vector = [RankedUnit(a, 1.0), RankedUnit(b, -1.0)]
    for _ in range(3):
        fused = fuse_rankings(iter(lexical), iter(vector))
        assert [hit.unit_id for hit in fused] == [a, b]
        assert fused[0].score == fused[1].score == 1 / 61 + 1 / 62
    assert fuse_rankings(lexical, ()) == (
        FusedUnit(b, 1 / 61, 1, None, -2.0, None),
        FusedUnit(a, 1 / 62, 2, None, -1.0, None),
    )
    assert fuse_rankings((), vector)[0] == FusedUnit(a, 1 / 61, None, 1, None, 1.0)
    assert fuse_rankings((), ()) == ()


@pytest.mark.parametrize("rrf_k", [0, -1, True, False, 1.5, "60", None])
def test_rrf_rejects_invalid_k(rrf_k: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        fuse_rankings((), (), rrf_k=rrf_k)


@pytest.mark.parametrize("mode", MODES)
def test_eligible_status_and_source_filter_before_route_limit(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    second = corpus.ingest(V2)
    stale, active, _ = second.units
    corpus.lifecycle.mark_stale(stale.id, reason="review required")
    # Old versions include superseded and deleted units, but falsely claim active.
    for unit in first.units:
        corpus.store(unit, status="active", text="forged old payload")
    corpus.store(stale, status="active", text="forged stale payload")
    corpus.store(active, (0.6, 0.8), status="deleted", text="forged active payload")
    corpus.store(second.units[2], (0.0, 1.0), status="stale")
    unknown_id = uuid4()
    corpus.index.upsert([VectorRecord(id=str(unknown_id), vector=(1.0, 0.0))])
    # Stronger cosine candidates from another source must also be excluded early.
    other = Corpus(tmp_path / "other", session_factory)
    other.path.parent.mkdir()
    other_result = other.ingest()
    for unit in other_result.units:
        corpus.store(unit)
    corpus.index.sealed = True
    result = corpus.retrieval.search(
        "target",
        mode=mode,
        top_k=1,
        candidate_limit=1,
        source_id=corpus.source.id,
    )
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.unit.id in {active.id, second.units[2].id}
    assert hit.status is KnowledgeUnitStatus.ACTIVE
    assert hit.unit.text != "forged active payload"
    assert hit.source == corpus.source
    assert hit.document == second.ingestion.document
    assert hit.unit.end_char > hit.unit.start_char
    assert not result.degraded
    if mode is RetrievalMode.VECTOR:
        assert hit.unit.id == active.id
        assert hit.vector_score == pytest.approx(0.6)
        assert hit.score == 1 / 61
        assert hit.lexical_score is None
    if mode is not RetrievalMode.LEXICAL:
        assert corpus.index.get_calls == [
            tuple(
                sorted(
                    [
                        str(active.id),
                        str(second.units[2].id),
                    ]
                )
            )
        ]
        assert corpus.provider.calls == [("target",)]
    else:
        assert corpus.provider.calls == []
        assert corpus.index.get_calls == []
    stale_result = corpus.retrieval.search(
        "target",
        mode=mode,
        top_k=10,
        include_stale=True,
        source_id=corpus.source.id,
    )
    assert ids(stale_result) == {unit.id for unit in second.units}
    stale_hit = next(hit for hit in stale_result.hits if hit.unit.id == stale.id)
    assert stale_hit.status is KnowledgeUnitStatus.STALE
    assert stale_hit.stale_reason == "review required"


@pytest.mark.parametrize("mode", MODES)
def test_asof_revert_and_missing_old_vectors(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    first_at = corpus.now()
    before_stale = corpus.now()
    corpus.lifecycle.mark_stale(first.units[1].id, reason="historical review")
    stale_at = corpus.now()
    corpus.lifecycle.refresh(first.units[1].id)
    second = corpus.ingest(V2)
    second_at = corpus.now()
    # Only the latest version remains indexed; history must not be auto-repaired.
    for unit in second.units:
        corpus.store(unit)
    corpus.index.sealed = True
    before = corpus.retrieval.search("target", mode=mode, at=NOW)
    assert before.hits == () and before.missing_vector_ids == ()
    historical = corpus.retrieval.search("target", mode=mode, at=first_at)
    wanted_missing = (
        () if mode is RetrievalMode.LEXICAL else tuple(sorted(u.id for u in first.units))
    )
    assert historical.missing_vector_ids == wanted_missing
    assert historical.degraded is (mode is not RetrievalMode.LEXICAL)
    if mode is RetrievalMode.VECTOR:
        assert historical.hits == ()
    else:
        assert ids(historical) == {u.id for u in first.units}
        assert all(hit.status is KnowledgeUnitStatus.ACTIVE for hit in historical.hits)
        assert all(hit.document == first.ingestion.document for hit in historical.hits)
    at_stale = corpus.retrieval.search("target", mode=mode, at=stale_at)
    if mode is not RetrievalMode.LEXICAL:
        assert first.units[1].id not in at_stale.missing_vector_ids
    if mode is not RetrievalMode.VECTOR:
        assert first.units[1].id not in ids(at_stale)
        with_stale = corpus.retrieval.search(
            "target",
            mode=mode,
            at=stale_at,
            include_stale=True,
        )
        stale_hit = next(hit for hit in with_stale.hits if hit.unit.id == first.units[1].id)
        assert stale_hit.stale_reason == "historical review"
    assert corpus.retrieval.search("target", mode=mode, at=before_stale) == historical
    current = corpus.retrieval.search("target", mode=mode)
    assert ids(current) == {u.id for u in second.units}
    assert current.evaluated_at == second_at
    assert not current.degraded
    reverted = corpus.ingest(V1)
    assert reverted.reverted
    assert reverted.ingestion.document.id == first.ingestion.document.id
    again = corpus.retrieval.search("target", mode=mode)
    assert again.missing_vector_ids == wanted_missing
    assert ids(again) == (set() if mode is RetrievalMode.VECTOR else {u.id for u in first.units})
    during_second = corpus.retrieval.search("target", mode=mode, at=second_at)
    assert ids(during_second) == {u.id for u in second.units}
    assert not during_second.degraded
    assert all(call == ("target",) for call in corpus.provider.calls)


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR, RetrievalMode.HYBRID])
def test_missing_hash_mismatch_and_legacy_no_hash_records(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    result = corpus.ingest()
    missing, mismatch, legacy = result.units
    corpus.store(mismatch, content_hash="wrong hash", status="active")
    corpus.index.upsert(
        [
            VectorRecord(
                id=str(legacy.id),
                vector=(1.0, 0.0),
                payload={"status": "deleted", "text": "forged", "start_char": -100},
            ),
        ]
    )
    corpus.index.sealed = True
    found = corpus.retrieval.search("target", mode=mode, top_k=1, candidate_limit=1)
    assert found.missing_vector_ids == tuple(sorted([missing.id, mismatch.id]))
    assert found.degraded
    if mode is RetrievalMode.VECTOR:
        assert ids(found) == {legacy.id}
    else:
        assert ids(corpus.retrieval.search("target", mode=mode)) == {u.id for u in result.units}
    hit = corpus.retrieval.search("target", mode=mode).hits[0]
    assert hit.unit.text != "forged"
    assert hit.unit.start_char >= 0
    assert corpus.provider.calls == [("target",)] * (2 if mode is RetrievalMode.VECTOR else 3)


@pytest.mark.parametrize("mode", MODES)
def test_read_only_one_uow_and_metadata_cache_not_reconciled(
    tmp_path: Path,
    engine: Engine,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    first_at = corpus.now()
    second = corpus.ingest(V2)
    for unit in (*first.units, *second.units):
        corpus.store(unit)
    # Deliberately corrupt cached status; the revision log still controls eligibility.
    with engine.begin() as connection:
        connection.execute(text("UPDATE knowledge_units SET status = 'deleted'"))
    corpus.index.sealed = True
    tables = (
        "sources",
        "documents",
        "knowledge_units",
        "revisions",
        "citations",
        "knowledge_units_fts",
    )

    def snapshot() -> tuple[tuple[Any, ...], ...]:
        with engine.connect() as connection:
            return tuple(
                tuple(connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all())
                for table in tables
            )

    before = snapshot()
    original_vectors = corpus.index.snapshot()
    writes: list[str] = []
    sessions: list[object] = []
    commits: list[object] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().split()[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "DROP",
            "ALTER",
            "REPLACE",
        }:
            writes.append(statement)

    def capture_begin(session: Any, transaction: Any, connection: Any) -> None:
        sessions.append(session)

    def capture_commit(session: Any) -> None:
        commits.append(session)

    event.listen(engine, "before_cursor_execute", capture_sql)
    event.listen(session_factory, "after_begin", capture_begin)
    event.listen(session_factory, "after_commit", capture_commit)
    try:
        found = corpus.retrieval.search("target", mode=mode, at=first_at)
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        event.remove(session_factory, "after_begin", capture_begin)
        event.remove(session_factory, "after_commit", capture_commit)
    assert ids(found) == {u.id for u in first.units}
    assert all(hit.status is KnowledgeUnitStatus.ACTIVE for hit in found.hits)
    assert len(sessions) == 1 and commits == [] and writes == []
    assert snapshot() == before
    assert corpus.index.snapshot() == original_vectors
    assert corpus.provider.calls == ([] if mode is RetrievalMode.LEXICAL else [("target",)])


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR, RetrievalMode.HYBRID])
@pytest.mark.parametrize("has_vectors", [True, False])
def test_provider_exceptions_propagate_without_lexical_fallback(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
    has_vectors: bool,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    imported = corpus.ingest()
    if has_vectors:
        for unit in imported.units:
            corpus.store(unit)
    error = RuntimeError("provider is unavailable")
    corpus.provider.error = error
    with pytest.raises(RuntimeError, match="provider is unavailable") as caught:
        corpus.retrieval.search("target", mode=mode)
    assert caught.value is error
    assert corpus.provider.calls == [("target",)]


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR, RetrievalMode.HYBRID])
def test_missing_vector_configuration_is_explicit(
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    service = RetrievalService(session_factory)
    assert service.indexing_service is None
    with pytest.raises(RetrievalConfigurationError, match="indexing_service"):
        service.search("target", mode=mode)
    assert issubclass(RetrievalConfigurationError, ValueError)
    assert service.search("target").hits == ()


@pytest.mark.parametrize("mode", MODES)
def test_empty_library_empty_head_and_unknown_source(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    for _ in range(2):
        found = corpus.retrieval.search("target", mode=mode)
        assert found.hits == () and not found.degraded and found.missing_vector_ids == ()
    corpus.ingest()
    unknown = corpus.retrieval.search("target", mode=mode, source_id=uuid4())
    assert unknown.hits == () and not unknown.degraded
    corpus.ingest(" \n")
    empty = corpus.retrieval.search("target", mode=mode)
    assert empty.hits == () and not empty.degraded
    assert corpus.provider.calls == []


@pytest.mark.parametrize("query", ["notpresent", "!!! ???"])
def test_lexical_no_hits_and_raw_bm25(
    tmp_path: Path,
    session_factory: SessionFactory,
    query: str,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    imported = corpus.ingest("# Guide\n中文检索支持服务 service_v2.\n")
    assert corpus.retrieval.search(query).hits == ()
    found = corpus.retrieval.search("中文检索 service_v2")
    assert ids(found) == {imported.units[0].id}
    with UnitOfWork(session_factory) as uow:
        raw = FullTextRepository(uow.session).search(
            "中文检索 service_v2",
            eligible_ids={imported.units[0].id},
            limit=1,
        )
    hit = found.hits[0]
    assert hit.lexical_score == raw[0].score < 0
    assert hit.lexical_rank == 1 and hit.vector_rank is None and hit.vector_score is None
    assert hit.score == 1 / 61
    assert corpus.provider.calls == []


class UnawareTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": " \t\n"},
        {"query": 1},
        {"query": True},
        {"query": None},
        {"query": b"target"},
        {"mode": "unknown"},
        {"mode": 1},
        {"mode": True},
        {"mode": None},
        {"top_k": True},
        {"top_k": False},
        {"top_k": 0},
        {"top_k": -1},
        {"top_k": 1.0},
        {"top_k": "5"},
        {"top_k": None},
        {"rrf_k": True},
        {"rrf_k": 0},
        {"rrf_k": -1},
        {"rrf_k": "60"},
        {"rrf_k": 1.5},
        {"rrf_k": None},
        {"candidate_limit": True},
        {"candidate_limit": 0},
        {"candidate_limit": -1},
        {"candidate_limit": "50"},
        {"candidate_limit": 5.0},
        {"candidate_limit": 4, "top_k": 5},
        {"at": datetime(2026, 11, 9)},
        {"at": NOW.replace(tzinfo=UnawareTimezone())},
        {"at": "2026-11-09T12:00:00Z"},
        {"at": 123},
        {"at": False},
        {"include_stale": 1},
        {"include_stale": "false"},
        {"include_stale": None},
        {"source_id": "not-a-uuid"},
        {"source_id": str(UUID(int=1))},
        {"source_id": 1},
    ],
)
def test_search_strict_validation(session_factory: SessionFactory, kwargs: dict[str, Any]) -> None:
    service = RetrievalService(session_factory, clock=lambda: NOW)
    arguments = {"query": "target", **kwargs}
    with pytest.raises(ValueError):
        cast(Any, service.search)(**arguments)


def test_bad_clock_is_rejected_and_explicit_at_does_not_call_clock(
    session_factory: SessionFactory,
) -> None:
    service = RetrievalService(session_factory, clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        service.search("target")
    assert service.search("target", at=NOW).evaluated_at == NOW


@pytest.mark.parametrize("mode", MODES)
def test_enum_normalization_result_metadata_defaults_and_models(
    tmp_path: Path,
    session_factory: SessionFactory,
    mode: RetrievalMode,
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    imported = corpus.ingest()
    for unit in imported.units:
        corpus.store(unit)
    assert corpus.retrieval.indexing_service is corpus.indexing
    result = cast(Any, corpus.retrieval.search)("  target  ", mode=mode.value, top_k=51, rrf_k=10)
    assert isinstance(result, RetrievalResult)
    assert result.mode is mode and result.query == "target"
    assert result.top_k == result.candidate_limit == 51 and result.rrf_k == 10
    assert result.evaluated_at == corpus.now() and not result.include_stale
    assert len(result.hits) == 3
    assert isinstance(result.hits, tuple) and result.missing_vector_ids == ()
    normal = corpus.retrieval.search("target", mode=mode)
    assert normal.top_k == 5 and normal.candidate_limit == 50 and normal.rrf_k == 60
    assert RetrievalResult.model_validate(normal.model_dump()) == normal
    assert {member.value for member in RetrievalMode} == {"lexical", "vector", "hybrid"}
    assert normal.model_config["frozen"] and normal.model_config["extra"] == "forbid"
    for model in (normal, normal.hits[0]):
        with pytest.raises(ValidationError):
            cast(Any, model).score = 100
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "unknown": "forbidden"})
    default_missing = RetrievalResult.model_validate(
        {key: value for key, value in normal.model_dump().items() if key != "missing_vector_ids"}
    )
    assert default_missing.missing_vector_ids == () and not default_missing.degraded
    assert isinstance(normal.hits[0], RetrievalHit)
