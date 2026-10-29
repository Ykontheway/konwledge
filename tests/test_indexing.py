"""Tests for the vector index layer and :class:`IndexingService`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from konwledge.domain.models import (
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.embedding import (
    EmbeddingService,
    EmbeddingVector,
    HashEmbeddingProvider,
    InMemoryEmbeddingCache,
)
from konwledge.indexing import (
    IndexingService,
    InMemoryVectorIndex,
    LanceVectorIndex,
    SearchHit,
    VectorRecord,
    build_vector_record,
    matches_filter,
)

DIMENSION = 16


def _make_unit(
    *,
    ordinal: int,
    text: str,
    status: KnowledgeUnitStatus = KnowledgeUnitStatus.ACTIVE,
    tags: set[str] | None = None,
    section_path: tuple[str, ...] = (),
    heading: str | None = None,
) -> KnowledgeUnit:
    document_id = UUID("12345678-1234-5678-1234-567812345678")
    unit_id = UUID(int=ordinal + 1)
    return KnowledgeUnit(
        id=unit_id,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + len(text),
        status=status,
        tags=tags or set(),
        section_path=section_path,
        heading=heading,
    )


def _make_record(unit_id: str, vector: Sequence[float], payload: Mapping[str, Any]) -> VectorRecord:
    return VectorRecord(id=unit_id, vector=tuple(vector), payload=dict(payload))


# ---------------------------------------------------------------------------
# matches_filter helpers
# ---------------------------------------------------------------------------


def test_matches_filter_scalar_and_in_operators() -> None:
    payload = {"status": "active", "tags": ["python", "indexing"], "ordinal": 2}
    assert matches_filter(payload, None)
    assert matches_filter(payload, {"status": "active"})
    assert not matches_filter(payload, {"status": "stale"})
    assert matches_filter(payload, {"tags": "python"})
    assert matches_filter(payload, {"tags": {"in": ["python", "docs"]}})
    assert not matches_filter(payload, {"tags": {"in": ["docs"]}})
    assert matches_filter(payload, {"status": {"not": ["stale", "deleted"]}})
    assert not matches_filter(payload, {"status": {"not": ["active"]}})


def test_matches_filter_rejects_unknown_operators() -> None:
    with pytest.raises(ValueError, match="unsupported filter operators"):
        matches_filter({"status": "active"}, {"status": {"regex": "a.*"}})


# ---------------------------------------------------------------------------
# InMemoryVectorIndex contract
# ---------------------------------------------------------------------------


def test_in_memory_index_rejects_bad_dimension() -> None:
    with pytest.raises(ValueError):
        InMemoryVectorIndex(dimension=0)


def test_in_memory_upsert_is_idempotent_per_id() -> None:
    index = InMemoryVectorIndex(dimension=4)
    first = _make_record("u1", (1.0, 0.0, 0.0, 0.0), {"status": "active"})
    second = _make_record("u1", (0.0, 1.0, 0.0, 0.0), {"status": "stale"})
    index.upsert([first])
    index.upsert([second])
    assert len(index) == 1
    [stored] = index.snapshot()
    assert stored.vector == second.vector
    assert stored.payload["status"] == "stale"


def test_in_memory_upsert_validates_vector_dimension() -> None:
    index = InMemoryVectorIndex(dimension=4)
    with pytest.raises(ValueError, match="dimension mismatch"):
        index.upsert([_make_record("u1", (1.0, 0.0), {})])


def test_in_memory_upsert_rejects_empty_id() -> None:
    index = InMemoryVectorIndex(dimension=4)
    with pytest.raises(ValueError, match="non-empty string"):
        index.upsert([_make_record("", (1.0, 0.0, 0.0, 0.0), {})])


def test_in_memory_delete_reports_removed_rows_only() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert(
        [
            _make_record("u1", (1.0, 0.0, 0.0, 0.0), {}),
            _make_record("u2", (0.0, 1.0, 0.0, 0.0), {}),
        ]
    )
    removed = index.delete(["u1", "u-missing"])
    assert removed == 1
    assert len(index) == 1


def test_in_memory_search_returns_top_k_sorted_desc() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert(
        [
            _make_record("u1", (1.0, 0.0, 0.0, 0.0), {"status": "active"}),
            _make_record("u2", (0.9, 0.1, 0.0, 0.0), {"status": "active"}),
            _make_record("u3", (0.0, 1.0, 0.0, 0.0), {"status": "active"}),
        ]
    )
    hits = index.search((1.0, 0.0, 0.0, 0.0), top_k=2)
    assert [hit.id for hit in hits] == ["u1", "u2"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].score >= hits[1].score


def test_in_memory_search_is_deterministic_across_runs() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert(
        [
            _make_record("u1", (1.0, 0.0, 0.0, 0.0), {}),
            _make_record("u2", (1.0, 0.0, 0.0, 0.0), {}),
            _make_record("u3", (1.0, 0.0, 0.0, 0.0), {}),
        ]
    )
    first = index.search((1.0, 0.0, 0.0, 0.0), top_k=3)
    second = index.search((1.0, 0.0, 0.0, 0.0), top_k=3)
    assert [hit.id for hit in first] == [hit.id for hit in second] == ["u1", "u2", "u3"]


def test_in_memory_search_applies_metadata_filters() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert(
        [
            _make_record("u1", (1.0, 0.0, 0.0, 0.0), {"status": "active"}),
            _make_record("u2", (0.9, 0.1, 0.0, 0.0), {"status": "stale"}),
        ]
    )
    hits = index.search((1.0, 0.0, 0.0, 0.0), top_k=5, filters={"status": "active"})
    assert [hit.id for hit in hits] == ["u1"]


def test_in_memory_search_respects_min_score() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert(
        [
            _make_record("u1", (1.0, 0.0, 0.0, 0.0), {}),
            _make_record("u2", (0.0, 1.0, 0.0, 0.0), {}),
        ]
    )
    hits = index.search((1.0, 0.0, 0.0, 0.0), top_k=5, min_score=0.5)
    assert [hit.id for hit in hits] == ["u1"]


def test_in_memory_search_rejects_bad_arguments() -> None:
    index = InMemoryVectorIndex(dimension=4)
    with pytest.raises(ValueError, match="top_k"):
        index.search((1.0, 0.0, 0.0, 0.0), top_k=0)
    with pytest.raises(ValueError, match="query dimension"):
        index.search((1.0, 0.0, 0.0), top_k=1)


def test_in_memory_search_handles_zero_query_vector() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert([_make_record("u1", (1.0, 0.0, 0.0, 0.0), {})])
    assert index.search((0.0, 0.0, 0.0, 0.0), top_k=5) == []


def test_in_memory_rebuild_replaces_contents_atomically() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert([_make_record("old", (1.0, 0.0, 0.0, 0.0), {})])
    index.rebuild(
        [
            _make_record("new-1", (1.0, 0.0, 0.0, 0.0), {}),
            _make_record("new-2", (0.0, 1.0, 0.0, 0.0), {}),
        ]
    )
    ids = {record.id for record in index.snapshot()}
    assert ids == {"new-1", "new-2"}


def test_in_memory_rebuild_rejects_bad_batch_without_mutation() -> None:
    index = InMemoryVectorIndex(dimension=4)
    index.upsert([_make_record("keep", (1.0, 0.0, 0.0, 0.0), {})])
    with pytest.raises(ValueError, match="dimension mismatch"):
        index.rebuild(
            [
                _make_record("ok", (1.0, 0.0, 0.0, 0.0), {}),
                _make_record("bad", (1.0,), {}),
            ]
        )
    ids = {record.id for record in index.snapshot()}
    assert ids == {"keep"}


# ---------------------------------------------------------------------------
# build_vector_record payload shape
# ---------------------------------------------------------------------------


def test_build_vector_record_captures_traceable_payload() -> None:
    unit = _make_unit(
        ordinal=3,
        text="hello",
        tags={"python", "indexing"},
        section_path=("Intro", "Overview"),
        heading="Overview",
    )
    record = build_vector_record(
        unit,
        vector=(1.0, 0.0, 0.0, 0.0),
        extra_payload={"document_locator": "docs/overview.md"},
    )
    assert record.id == str(unit.id)
    assert record.payload["document_id"] == str(unit.document_id)
    assert record.payload["ordinal"] == 3
    assert record.payload["status"] == "active"
    assert record.payload["section_path"] == ["Intro", "Overview"]
    assert record.payload["tags"] == ["indexing", "python"]
    assert record.payload["document_locator"] == "docs/overview.md"


# ---------------------------------------------------------------------------
# IndexingService
# ---------------------------------------------------------------------------


def _build_indexing_service() -> IndexingService:
    provider = HashEmbeddingProvider(dimension=DIMENSION)
    service = EmbeddingService(provider, cache=InMemoryEmbeddingCache())
    index = InMemoryVectorIndex(dimension=DIMENSION)
    return IndexingService(embedding_service=service, vector_index=index)


def test_indexing_service_rejects_dimension_mismatch() -> None:
    provider = HashEmbeddingProvider(dimension=DIMENSION)
    service = EmbeddingService(provider)
    index = InMemoryVectorIndex(dimension=DIMENSION + 1)
    with pytest.raises(ValueError, match="does not match embedding provider"):
        IndexingService(embedding_service=service, vector_index=index)


def test_indexing_service_persists_units_and_search_is_stable() -> None:
    service = _build_indexing_service()
    units = [
        _make_unit(ordinal=0, text="python testing patterns"),
        _make_unit(ordinal=1, text="rust async executors"),
        _make_unit(ordinal=2, text="python typing deep dive"),
    ]
    service.index_units(units)
    first = service.search("python testing patterns", top_k=1)
    second = service.search("python testing patterns", top_k=1)
    assert first == second
    assert first[0].id == str(units[0].id)
    assert first[0].score == pytest.approx(1.0)


def test_indexing_service_upsert_is_idempotent_over_runs() -> None:
    service = _build_indexing_service()
    units = [_make_unit(ordinal=i, text=f"unit {i}") for i in range(5)]
    service.index_units(units)
    service.index_units(units)
    assert len(service.vector_index) == len(units)


def test_indexing_service_remove_units_deletes_vectors() -> None:
    service = _build_indexing_service()
    units = [_make_unit(ordinal=i, text=f"unit {i}") for i in range(3)]
    service.index_units(units)
    removed = service.remove_units([units[0].id, units[1].id])
    assert removed == 2
    assert len(service.vector_index) == 1


def test_indexing_service_rebuild_replaces_existing_vectors() -> None:
    service = _build_indexing_service()
    stale = [_make_unit(ordinal=i, text=f"old {i}") for i in range(3)]
    fresh = [_make_unit(ordinal=10 + i, text=f"new {i}") for i in range(2)]
    service.index_units(stale)
    service.rebuild(fresh)
    assert len(service.vector_index) == len(fresh)
    hits = service.search("new 0", top_k=1)
    assert hits[0].id == str(fresh[0].id)


def test_indexing_service_filters_by_payload_metadata() -> None:
    service = _build_indexing_service()
    active = _make_unit(ordinal=0, text="shared query", status=KnowledgeUnitStatus.ACTIVE)
    stale = _make_unit(ordinal=1, text="shared query", status=KnowledgeUnitStatus.STALE)
    service.index_units([active, stale])
    hits = service.search("shared query", top_k=5, filters={"status": "active"})
    assert [hit.id for hit in hits] == [str(active.id)]


def test_indexing_service_search_rejects_empty_query() -> None:
    service = _build_indexing_service()
    service.index_units([_make_unit(ordinal=0, text="hi")])
    with pytest.raises(ValueError, match="non-whitespace"):
        service.search("   ", top_k=1)


def test_indexing_service_empty_rebuild_empties_index() -> None:
    service = _build_indexing_service()
    service.index_units([_make_unit(ordinal=0, text="hi")])
    service.rebuild([])
    assert len(service.vector_index) == 0


# ---------------------------------------------------------------------------
# Optional LanceDB adapter -- only runs when the dependency is installed
# ---------------------------------------------------------------------------


def _require_lancedb() -> Any:
    return pytest.importorskip("lancedb")


def test_lance_vector_index_roundtrip(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_lancedb()
    index = LanceVectorIndex(uri=tmp_path / "lance.db", dimension=4)
    records = [
        _make_record("u1", (1.0, 0.0, 0.0, 0.0), {"status": "active"}),
        _make_record("u2", (0.0, 1.0, 0.0, 0.0), {"status": "stale"}),
    ]
    index.upsert(records)
    assert len(index) == 2
    hits = index.search((1.0, 0.0, 0.0, 0.0), top_k=1, filters={"status": "active"})
    assert hits and hits[0].id == "u1"
    removed = index.delete(["u1", "missing"])
    assert removed == 1
    index.rebuild([_make_record("u3", (1.0, 0.0, 0.0, 0.0), {})])
    assert len(index) == 1


def test_lance_vector_index_uses_in_memory_dependency_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``lancedb`` is missing, the error names the install hint."""

    import konwledge.indexing.lance as lance_module

    original_connect = lance_module.LanceVectorIndex._connect

    def _raise_import_error(self: Any, uri: str) -> Any:
        raise ImportError(lance_module._INSTALL_HINT)

    monkeypatch.setattr(lance_module.LanceVectorIndex, "_connect", _raise_import_error)
    try:
        with pytest.raises(ImportError, match="LanceDB is an optional dependency"):
            lance_module.LanceVectorIndex(uri="/tmp/does-not-matter", dimension=4)
    finally:
        monkeypatch.setattr(lance_module.LanceVectorIndex, "_connect", original_connect)


# ---------------------------------------------------------------------------
# Fixtures & smoke references kept to make tests self-documenting
# ---------------------------------------------------------------------------


def test_knowledge_unit_fixture_shape() -> None:
    """Sanity check that the test unit factory produces valid domain objects."""

    source = Source(
        id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        kind=SourceKind.MANUAL,
        uri="manual://fixture",
        created_at=datetime.now(tz=UTC),
    )
    unit = _make_unit(ordinal=0, text="hello")
    assert isinstance(unit, KnowledgeUnit)
    assert source.kind is SourceKind.MANUAL
    assert isinstance(SearchHit(id="x", score=0.0), SearchHit)


def test_embedding_provider_still_imports() -> None:
    """Lightweight guard against accidental module layout regressions."""

    assert HashEmbeddingProvider(dimension=DIMENSION).dimension == DIMENSION
    assert isinstance(DocumentContentType.MARKDOWN, DocumentContentType)
    vector: EmbeddingVector = (0.0,) * DIMENSION
    assert len(vector) == DIMENSION
