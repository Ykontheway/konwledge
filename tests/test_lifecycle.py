"""U09: document diff, lineage, revision chains, and partial index rebuilds."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, inspect, text
from typer.testing import CliRunner

from konwledge.application import KnowledgeLibrary
from konwledge.cli import app
from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.embedding import EmbeddingService, EmbeddingVector, HashEmbeddingProvider
from konwledge.indexing import IndexingService, InMemoryVectorIndex, LanceVectorIndex
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionResult,
    TraceableIngestionService,
    fingerprint_text,
)
from konwledge.lifecycle import (
    DocumentDiff,
    UnitChangeKind,
    diff_units,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
DOC_A = UUID("a0000000-0000-0000-0000-000000000001")
DOC_B = UUID("b0000000-0000-0000-0000-000000000002")

V1 = "# Guide\nIntro text.\n\n## Install\nRun uv sync.\n\n## Usage\nRun the CLI.\n"
V2 = (
    "# Guide\nIntro text.\n\n## Usage\nRun the CLI with --database.\n\n"
    "## FAQ\nAsk in issues.\n\n## Install\nRun uv sync.\n\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def unit(
    ordinal: int,
    body: str,
    *,
    section: tuple[str, ...] = (),
    document_id: UUID = DOC_A,
) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=uuid4(),
        document_id=document_id,
        ordinal=ordinal,
        text=body,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + len(body),
        section_path=section,
    )


class CountingProvider:
    """Hash provider that records every text it is asked to embed."""

    def __init__(self, dimension: int = 16) -> None:
        self._inner = HashEmbeddingProvider(dimension=dimension)
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        self.calls.extend(texts)
        return self._inner.embed(texts)


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
    """One file source that can be rewritten and re-ingested with a moving clock."""

    def __init__(self, tmp_path: Path, session_factory: SessionFactory) -> None:
        self.path = tmp_path / "guide.md"
        self.source_id = uuid4()
        self._session_factory = session_factory
        self._tick = 0
        self.path.write_text(V1, encoding="utf-8")
        with UnitOfWork(session_factory) as uow:
            uow.sources.add(
                Source(
                    id=self.source_id,
                    kind=SourceKind.FILE,
                    uri=str(self.path),
                    created_at=NOW,
                )
            )
            uow.commit()
        self.service = TraceableIngestionService(
            FileIngestionService(session_factory, clock=self._clock),
            KnowledgeUnitService(session_factory, clock=self._clock),
        )

    def _clock(self) -> datetime:
        self._tick += 1
        return NOW + timedelta(minutes=self._tick)

    def ingest(self, content: str | None = None) -> TraceableIngestionResult:
        if content is not None:
            self.path.write_text(content, encoding="utf-8")
        return self.service.ingest(self.source_id)


# ---------------------------------------------------------------------------
# Domain: lineage and content hash
# ---------------------------------------------------------------------------


def test_lineage_defaults_to_unit_id_and_content_hash_is_stable() -> None:
    first = unit(0, "Same text.")
    explicit = KnowledgeUnit.model_validate(
        {**first.model_dump(), "id": uuid4(), "lineage_id": first.lineage_id}
    )

    assert first.lineage_id == first.id
    assert explicit.lineage_id == first.id
    assert explicit.content_hash == first.content_hash
    assert len(first.content_hash) == 64
    assert KnowledgeUnit.model_validate_json(first.model_dump_json()) == first


def test_nil_lineage_is_rejected() -> None:
    with pytest.raises(ValueError, match="nil UUID"):
        KnowledgeUnit(
            id=uuid4(),
            lineage_id=UUID(int=0),
            document_id=DOC_A,
            ordinal=0,
            text="x",
            start_char=0,
            end_char=1,
        )


# ---------------------------------------------------------------------------
# Diff algorithm
# ---------------------------------------------------------------------------


def test_identical_versions_are_a_noop() -> None:
    old = [unit(0, "Alpha.", section=("A",)), unit(1, "Beta.", section=("B",))]
    new = [
        unit(0, "Alpha.", section=("A",), document_id=DOC_B),
        unit(1, "Beta.", section=("B",), document_id=DOC_B),
    ]

    diff = diff_units(old, new)

    assert diff.is_noop
    assert diff.affected == ()
    assert diff.summary() == "+0 ~0 >0 =2 -0"


def test_diff_classifies_every_change_kind() -> None:
    old = [
        unit(0, "Intro text.", section=("Guide",)),
        unit(1, "Run uv sync.", section=("Guide", "Install")),
        unit(2, "Run the CLI.", section=("Guide", "Usage")),
        unit(3, "Legacy notes nobody reads.", section=("Guide", "Legacy")),
    ]
    new = [
        unit(0, "Intro text.", section=("Guide",), document_id=DOC_B),
        unit(1, "Run the CLI with --database.", section=("Guide", "Usage"), document_id=DOC_B),
        unit(2, "Ask in issues.", section=("Guide", "FAQ"), document_id=DOC_B),
        unit(3, "Run uv sync.", section=("Guide", "Setup"), document_id=DOC_B),
    ]

    diff = diff_units(old, new)

    kinds = [(change.kind, change.new and change.new.text) for change in diff.changes]
    assert kinds == [
        (UnitChangeKind.UNCHANGED, "Intro text."),
        (UnitChangeKind.MODIFIED, "Run the CLI with --database."),
        (UnitChangeKind.ADDED, "Ask in issues."),
        (UnitChangeKind.MOVED, "Run uv sync."),
        (UnitChangeKind.REMOVED, None),
    ]
    assert diff.of_kind(UnitChangeKind.REMOVED)[0].old == old[3]
    assert diff.of_kind(UnitChangeKind.MODIFIED)[0].old == old[2]
    assert 0.0 < diff.of_kind(UnitChangeKind.MODIFIED)[0].similarity < 1.0
    assert [change.new.text for change in diff.affected if change.new] == [
        "Run the CLI with --database.",
        "Ask in issues.",
    ]
    assert diff.counts() == {
        "added": 1,
        "modified": 1,
        "moved": 1,
        "unchanged": 1,
        "removed": 1,
    }


def test_same_section_rewrite_counts_as_modification_below_threshold() -> None:
    old = [unit(0, "aaaa", section=("Notes",))]
    new = [unit(0, "zzzz", section=("Notes",), document_id=DOC_B)]

    diff = diff_units(old, new)

    assert [change.kind for change in diff.changes] == [UnitChangeKind.MODIFIED]


def test_unrelated_rewrite_without_section_is_add_plus_remove() -> None:
    old = [unit(0, "aaaa")]
    new = [unit(0, "zzzz", document_id=DOC_B)]

    diff = diff_units(old, new)

    assert [change.kind for change in diff.changes] == [
        UnitChangeKind.ADDED,
        UnitChangeKind.REMOVED,
    ]


def test_inherited_lineage_links_matched_units_only() -> None:
    old = [unit(0, "Keep me.", section=("A",)), unit(1, "Drop me.", section=("B",))]
    new = [
        unit(0, "Keep me.", section=("A",), document_id=DOC_B),
        unit(1, "Brand new.", section=("C",), document_id=DOC_B),
    ]

    diff = diff_units(old, new).with_inherited_lineage()

    kept, added = diff.new_units
    assert kept.lineage_id == old[0].lineage_id
    assert kept.id != old[0].id
    assert added.lineage_id == added.id


def test_diff_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError, match="similarity_threshold"):
        diff_units([], [], similarity_threshold=1.5)


# ---------------------------------------------------------------------------
# Lineage-aware ingestion and revision chains
# ---------------------------------------------------------------------------


def test_reingesting_changed_file_links_lineages_and_appends_revisions(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)

    first = corpus.ingest()
    second = corpus.ingest(V2)
    third = corpus.ingest()  # same content again: idempotent

    assert first.diff is None
    assert second.diff is not None
    assert second.previous_document_id == first.ingestion.document.id
    # Install was reordered but kept its text and section, so it is unchanged.
    assert second.diff.summary() == "+1 ~1 >0 =2 -0"
    assert third.diff is None and third.units_created is False

    old_by_text = {item.text: item for item in first.units}
    by_kind = {change.kind: change for change in second.diff.changes}
    modified = by_kind[UnitChangeKind.MODIFIED]
    assert modified.new is not None and modified.old is not None
    assert modified.new.lineage_id == modified.old.lineage_id
    reordered = next(
        change
        for change in second.diff.of_kind(UnitChangeKind.UNCHANGED)
        if change.new is not None and change.new.section_path == ("Guide", "Install")
    )
    assert reordered.new is not None and reordered.old is not None
    assert reordered.new.ordinal != reordered.old.ordinal
    assert reordered.new.lineage_id == old_by_text[reordered.new.text].lineage_id

    with UnitOfWork(session_factory) as uow:
        persisted = tuple(uow.knowledge_units.list_by_document(second.ingestion.document.id))
        chain = uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, modified.new.lineage_id)
        unchanged = by_kind[UnitChangeKind.UNCHANGED].new
        assert unchanged is not None
        quiet_chain = uow.revisions.list_for(
            RevisionEntityType.KNOWLEDGE_UNIT, unchanged.lineage_id
        )
        total_revisions = len(uow.revisions.list())

    assert persisted == second.units
    assert [(rev.revision, rev.change_type) for rev in chain] == [
        (1, RevisionChangeType.CREATE),
        (2, RevisionChangeType.UPDATE),
    ]
    assert chain[1].payload["change"] == "modified"
    assert chain[1].payload["previous_unit_id"] == str(modified.old.id)
    assert chain[1].payload["unit_id"] == str(modified.new.id)
    assert [rev.change_type for rev in quiet_chain] == [RevisionChangeType.CREATE]
    # 3 creates for v1, then added + modified for v2; reimport adds nothing.
    assert total_revisions == 5


def test_removed_units_close_their_lineage(tmp_path: Path, session_factory: SessionFactory) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    second = corpus.ingest("# Guide\nIntro text.\n")

    assert second.diff is not None
    removed = second.diff.of_kind(UnitChangeKind.REMOVED)
    assert {change.old.text for change in removed if change.old} == {
        "## Install\nRun uv sync.\n\n",
        "## Usage\nRun the CLI.\n",
    }
    with UnitOfWork(session_factory) as uow:
        for change in removed:
            assert change.old is not None
            latest = uow.revisions.latest_for(
                RevisionEntityType.KNOWLEDGE_UNIT, change.old.lineage_id
            )
            assert latest is not None
            assert latest.change_type is RevisionChangeType.DELETE
            assert latest.payload["document_id"] == str(second.ingestion.document.id)
    assert len(first.units) == 3


def test_emptied_document_records_deletes_once(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    corpus.ingest()

    emptied = corpus.ingest("   \n")
    again = corpus.ingest()

    assert emptied.units == ()
    assert emptied.diff is not None
    assert emptied.diff.summary() == "+0 ~0 >0 =0 -3"
    assert again.diff is None
    with UnitOfWork(session_factory) as uow:
        deletes = [
            rev for rev in uow.revisions.list() if rev.change_type is RevisionChangeType.DELETE
        ]
    assert len(deletes) == 3


def test_library_history_lists_versions_and_revisions(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    corpus.ingest()
    second = corpus.ingest(V2)
    assert second.diff is not None
    modified = second.diff.of_kind(UnitChangeKind.MODIFIED)[0]
    assert modified.new is not None and modified.old is not None

    history = KnowledgeLibrary(session_factory).unit_history(modified.new.id)

    assert history.lineage_id == modified.old.lineage_id
    assert [version.id for version in history.versions] == [modified.old.id, modified.new.id]
    assert [revision.revision for revision in history.revisions] == [1, 2]


# ---------------------------------------------------------------------------
# Partial index rebuild (the U09 acceptance criterion)
# ---------------------------------------------------------------------------


def test_apply_diff_only_embeds_changed_units(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    provider = CountingProvider()
    # No embedding cache on purpose: reuse must come from the index itself.
    indexing = IndexingService(
        embedding_service=EmbeddingService(provider),
        vector_index=InMemoryVectorIndex(dimension=provider.dimension),
    )
    first = corpus.ingest()
    indexing.index_units(first.units)
    provider.calls.clear()

    second = corpus.ingest(V2)
    assert second.diff is not None
    report = indexing.apply_diff(second.diff)

    assert provider.calls == [change.new.text for change in second.diff.affected if change.new]
    assert len(report.embedded) == 2
    assert len(report.reused) == 2
    assert report.deleted == len(first.units)
    assert report.upserted == len(second.units)

    index = indexing.vector_index
    assert len(index) == len(second.units)
    assert index.get([str(item.id) for item in first.units]) == {}
    stored = index.get([str(item.id) for item in second.units])
    old_vectors = {item.content_hash: item for item in first.units}
    for item in second.units:
        record = stored[str(item.id)]
        assert record.payload["document_id"] == str(second.ingestion.document.id)
        assert record.payload["lineage_id"] == str(item.lineage_id)
        assert record.vector == HashEmbeddingProvider(dimension=16).embed([item.text])[0]
        if item.content_hash in old_vectors:
            assert str(item.id) in report.reused


def test_apply_diff_embeds_reusable_units_missing_from_index() -> None:
    old = [unit(0, "Keep me.", section=("A",))]
    new = [unit(0, "Keep me.", section=("A",), document_id=DOC_B)]
    provider = CountingProvider()
    indexing = IndexingService(
        embedding_service=EmbeddingService(provider),
        vector_index=InMemoryVectorIndex(dimension=provider.dimension),
    )

    report = indexing.apply_diff(diff_units(old, new).with_inherited_lineage())

    assert provider.calls == ["Keep me."]
    assert report.embedded == (str(new[0].id),)
    assert report.deleted == 0


def test_apply_noop_diff_does_not_embed() -> None:
    old = [unit(0, "Keep me.", section=("A",))]
    provider = CountingProvider()
    indexing = IndexingService(
        embedding_service=EmbeddingService(provider),
        vector_index=InMemoryVectorIndex(dimension=provider.dimension),
    )
    indexing.index_units(old)
    provider.calls.clear()

    report = indexing.apply_diff(DocumentDiff(changes=()))

    assert provider.calls == []
    assert report.upserted == 0 and report.deleted == 0
    assert len(indexing.vector_index) == 1


# ---------------------------------------------------------------------------
# Storage upgrades and CLI
# ---------------------------------------------------------------------------


def test_create_schema_upgrades_pre_u09_database(tmp_path: Path) -> None:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    create_schema(engine)
    legacy_unit = uuid4()
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_knowledge_units_lineage_id"))
        connection.execute(text("ALTER TABLE knowledge_units DROP COLUMN lineage_id"))
    session_factory = create_session_factory(engine)
    source_id = uuid4()
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(Source(id=source_id, kind=SourceKind.FILE, uri="legacy.md", created_at=NOW))
        uow.documents.add(
            Document(
                id=DOC_A,
                source_id=source_id,
                locator="legacy.md",
                content_type=DocumentContentType.MARKDOWN,
                checksum=fingerprint_text("legacy"),
                ingested_at=NOW,
            )
        )
        uow.commit()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_units (id, document_id, ordinal, text, start_char, "
                "end_char, section_path, status, tags, metadata) VALUES (:id, :doc, 0, "
                "'legacy', 0, 6, '[]', 'active', '[]', '{}')"
            ),
            {"id": legacy_unit.hex, "doc": DOC_A.hex},
        )

    create_schema(engine)
    create_schema(engine)  # idempotent

    columns = {column["name"] for column in inspect(engine).get_columns("knowledge_units")}
    indexes = {index["name"] for index in inspect(engine).get_indexes("knowledge_units")}
    assert "lineage_id" in columns
    assert "ix_knowledge_units_lineage_id" in indexes
    with UnitOfWork(session_factory) as uow:
        restored = uow.knowledge_units.get(legacy_unit)
        assert restored is not None
        assert restored.lineage_id == legacy_unit
        assert [item.id for item in uow.knowledge_units.list_by_lineage(legacy_unit)] == [
            legacy_unit
        ]
    engine.dispose()


def test_cli_reports_updates_and_history(tmp_path: Path) -> None:
    runner = CliRunner()
    database = tmp_path / "knowledge.db"
    document = tmp_path / "guide.md"
    document.write_text(V1, encoding="utf-8")

    first = runner.invoke(app, ["import", str(document), "-d", str(database)])
    document.write_text(V2, encoding="utf-8")
    second = runner.invoke(app, ["import", str(document), "-d", str(database)])

    assert first.exit_code == 0 and first.stdout.startswith("created:")
    assert second.exit_code == 0
    assert second.stdout.startswith("updated:")
    assert "changes=+1 ~1 >0 =2 -0 | re-embed=2" in second.stdout

    listing = runner.invoke(app, ["list", "-d", str(database)]).stdout
    usage_line = next(line for line in listing.splitlines() if "section=Guide / Usage" in line)
    unit_id = usage_line.split()[1]
    history = runner.invoke(app, ["history", unit_id, "-d", str(database)])

    assert history.exit_code == 0
    assert "versions: 2" in history.stdout
    assert "rev 1 create (added)" in history.stdout
    assert "rev 2 update (modified)" in history.stdout

    missing = runner.invoke(app, ["history", str(uuid4()), "-d", str(database)])
    assert missing.exit_code == 1


# ---------------------------------------------------------------------------
# Optional LanceDB coverage for the U08 review fixes
# ---------------------------------------------------------------------------


def test_lance_index_supports_get_filters_and_empty_rebuild(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from konwledge.indexing import VectorRecord

    index = LanceVectorIndex(uri=tmp_path / "lance", dimension=2)
    records = [
        VectorRecord(id=f"r{n:02d}", vector=(1.0, n / 100), payload={"keep": n == 19})
        for n in range(20)
    ]
    index.upsert(records)

    hits = index.search((1.0, 0.0), top_k=1, filters={"keep": True})
    assert [hit.id for hit in hits] == ["r19"]
    assert set(index.get(["r01", "missing"])) == {"r01"}
    assert index.get(["r01"])["r01"].payload == {"keep": False}

    index.rebuild([])
    assert len(index) == 0
    index.upsert(records[:1])
    assert len(index) == 1

    with pytest.raises(ValueError, match="dimension 2, expected 3"):
        LanceVectorIndex(uri=tmp_path / "lance", dimension=3)
