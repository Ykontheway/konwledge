"""U10: knowledge statuses, publication timelines, reverts, and as-of queries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from konwledge.application import KnowledgeLibrary, KnowledgeUnitNotFoundError
from konwledge.cli import app, main
from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider
from konwledge.indexing import IndexingService, InMemoryVectorIndex
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionResult,
    TraceableIngestionService,
    fingerprint_text,
)
from konwledge.lifecycle import (
    DocumentNotFoundError,
    LifecycleService,
    LifecycleTransitionError,
    Publication,
    UnitChangeKind,
    build_timeline,
    diff_by_lineage,
    head_at,
    load_timeline,
    resolve_status,
    stale_mark_at,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
DOC_A = UUID("a0000000-0000-0000-0000-000000000001")
DOC_B = UUID("b0000000-0000-0000-0000-000000000002")
SOURCE = UUID("c0000000-0000-0000-0000-000000000003")

V1 = "# Guide\nIntro text.\n\n## Install\nRun uv sync.\n\n## Usage\nRun the CLI.\n"
V2 = "# Guide\nIntro text.\n\n## Usage\nRun the CLI with --database.\n\n## FAQ\nAsk in issues.\n"
V3 = "# Guide\nIntro text.\n\n## Install\nRun uv sync --locked.\n\n## Usage\nRun the CLI.\n"

ACTIVE = KnowledgeUnitStatus.ACTIVE
STALE = KnowledgeUnitStatus.STALE
SUPERSEDED = KnowledgeUnitStatus.SUPERSEDED
DELETED = KnowledgeUnitStatus.DELETED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def unit(
    ordinal: int,
    body: str,
    *,
    section: tuple[str, ...] = (),
    document_id: UUID = DOC_A,
    lineage_id: UUID | None = None,
) -> KnowledgeUnit:
    return KnowledgeUnit.model_validate(
        {
            "id": uuid4(),
            "lineage_id": lineage_id,  # None starts a new lineage
            "document_id": document_id,
            "ordinal": ordinal,
            "text": body,
            "start_char": ordinal * 100,
            "end_char": ordinal * 100 + len(body),
            "section_path": section,
        }
    )


def document(document_id: UUID, minutes: int) -> Document:
    return Document(
        id=document_id,
        source_id=SOURCE,
        locator="guide.md",
        content_type=DocumentContentType.MARKDOWN,
        checksum=fingerprint_text(str(document_id)),
        ingested_at=NOW + timedelta(minutes=minutes),
    )


def revision(
    number: int,
    change: str,
    minutes: int,
    *,
    entity_type: RevisionEntityType = RevisionEntityType.KNOWLEDGE_UNIT,
    entity_id: UUID = DOC_A,
    **payload: object,
) -> Revision:
    return Revision(
        id=uuid4(),
        entity_type=entity_type,
        entity_id=entity_id,
        revision=number,
        change_type=RevisionChangeType.UPDATE,
        created_at=NOW + timedelta(minutes=minutes),
        payload={"change": change, **payload},
    )


def at(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


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
    """One file source rewritten over time; every clock call advances one minute."""

    def __init__(self, tmp_path: Path, session_factory: SessionFactory) -> None:
        self.path = tmp_path / "guide.md"
        self.source_id = uuid4()
        self.session_factory = session_factory
        self.tick = 0
        self.path.write_text(V1, encoding="utf-8")
        with UnitOfWork(session_factory) as uow:
            uow.sources.add(
                Source(id=self.source_id, kind=SourceKind.FILE, uri=str(self.path), created_at=NOW)
            )
            uow.commit()
        self.service = TraceableIngestionService(
            FileIngestionService(session_factory, clock=self.clock),
            KnowledgeUnitService(session_factory, clock=self.clock),
        )
        self.lifecycle = LifecycleService(session_factory, clock=self.clock)

    def clock(self) -> datetime:
        self.tick += 1
        return NOW + timedelta(minutes=self.tick)

    def now(self) -> datetime:
        return NOW + timedelta(minutes=self.tick)

    def ingest(self, content: str | None = None) -> TraceableIngestionResult:
        if content is not None:
            self.path.write_text(content, encoding="utf-8")
        return self.service.ingest(self.source_id)

    def statuses(self, at_time: datetime | None = None) -> dict[str, KnowledgeUnitStatus]:
        """Map unit text (first line after the heading) to status for every visible version."""

        snapshot = self.lifecycle.snapshot(at_time, include_retired=True)
        result: dict[str, KnowledgeUnitStatus] = {}
        for source in snapshot.sources:
            for state in (*source.units, *source.retired):
                result[f"{state.unit.document_id}:{state.unit.text.strip()}"] = state.status
        return result

    def effective_texts(self, at_time: datetime | None = None) -> list[str]:
        snapshot = self.lifecycle.snapshot(at_time)
        return [state.unit.text.strip() for state in snapshot.effective_units]

    def cached(self) -> dict[UUID, KnowledgeUnitStatus]:
        with UnitOfWork(self.session_factory) as uow:
            return {item.id: item.status for item in uow.knowledge_units.list()}


def by_text(units: tuple[KnowledgeUnit, ...], fragment: str) -> KnowledgeUnit:
    return next(item for item in units if fragment in item.text)


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def test_timeline_prefers_explicit_publications_and_collapses_repeats() -> None:
    documents = [document(DOC_B, 5), document(DOC_A, 1)]
    implicit = build_timeline(documents, [])
    assert [item.document_id for item in implicit] == [DOC_A, DOC_B]
    assert build_timeline(documents, [], unpublished=DOC_B) == (Publication(DOC_A, at(1)),)

    published = [
        revision(1, "published", 1, entity_type=RevisionEntityType.SOURCE, document_id=str(DOC_A)),
        revision(2, "published", 2, entity_type=RevisionEntityType.SOURCE, document_id=str(DOC_A)),
        revision(3, "published", 5, entity_type=RevisionEntityType.SOURCE, document_id=str(DOC_B)),
        revision(4, "renamed", 6, entity_type=RevisionEntityType.SOURCE, document_id=str(DOC_B)),
        revision(
            5,
            "published",
            9,
            entity_type=RevisionEntityType.SOURCE,
            document_id=str(DOC_A),
            reverted=True,
        ),
    ]
    explicit = build_timeline(documents, published)
    assert explicit == (
        Publication(DOC_A, at(1)),
        Publication(DOC_B, at(5)),
        Publication(DOC_A, at(9), reverted=True),
    )


def test_head_at_is_inclusive_and_none_before_first_publication() -> None:
    timeline = (Publication(DOC_A, at(1)), Publication(DOC_B, at(5)))

    assert head_at((), None) is None
    assert head_at(timeline) == timeline[-1]
    assert head_at(timeline, at(0.5)) is None
    assert head_at(timeline, at(1)) == timeline[0]
    assert head_at(timeline, at(4.9)) == timeline[0]
    assert head_at(timeline, at(5)) == timeline[1]


def test_stale_mark_lasts_until_the_next_lineage_revision() -> None:
    chain = [
        revision(1, "added", 1),
        revision(2, "stale", 3, reason="outdated"),
        revision(3, "modified", 6),
        revision(4, "stale", 8, reason="again"),
        revision(5, "refreshed", 9),
    ]

    assert stale_mark_at([], None) is None
    assert stale_mark_at(chain, at(2)) is None
    assert stale_mark_at(chain, at(3)) == chain[1]
    assert stale_mark_at(chain, at(5)) == chain[1]
    assert stale_mark_at(chain, at(7)) is None
    assert stale_mark_at(chain, at(8.5)) == chain[3]
    assert stale_mark_at(chain) is None
    assert stale_mark_at(chain[:4]) == chain[3]


@pytest.mark.parametrize(
    ("unit_document", "lineage_in_head", "published", "stale", "expected"),
    [
        (DOC_B, True, True, False, ACTIVE),
        (DOC_B, True, True, True, STALE),
        (DOC_A, True, True, False, SUPERSEDED),
        (DOC_A, True, True, True, SUPERSEDED),
        (DOC_A, False, True, False, DELETED),
        (DOC_A, False, False, False, None),
    ],
)
def test_resolve_status_table(
    unit_document: UUID,
    lineage_in_head: bool,
    published: bool,
    stale: bool,
    expected: KnowledgeUnitStatus | None,
) -> None:
    subject = unit(0, "Knowledge.", document_id=unit_document)
    lineages = {subject.lineage_id} if lineage_in_head else set()

    status = resolve_status(
        subject,
        head=Publication(DOC_B, at(5)),
        head_lineages=lineages,
        published=published,
        stale=stale,
    )

    assert status == expected
    assert resolve_status(subject, head=None, head_lineages=lineages, published=True) is None


def test_diff_by_lineage_matches_persisted_versions() -> None:
    same = unit(0, "Intro.", section=("Guide",))
    moved = unit(1, "Run uv sync.", section=("Guide", "Install"))
    edited = unit(2, "Run the CLI with --database.", section=("Guide", "Usage"))
    gone = unit(3, "FAQ.", section=("Guide", "FAQ"))
    old = [same, moved, edited, gone]
    new = [
        unit(0, "Intro.", section=("Guide",), document_id=DOC_B, lineage_id=same.lineage_id),
        unit(1, "Brand new.", section=("Guide", "New"), document_id=DOC_B),
        unit(
            2,
            "Run uv sync.",
            section=("Guide", "Setup"),
            document_id=DOC_B,
            lineage_id=moved.lineage_id,
        ),
        unit(
            3,
            "Run the CLI.",
            section=("Guide", "Usage"),
            document_id=DOC_B,
            lineage_id=edited.lineage_id,
        ),
    ]

    diff = diff_by_lineage(old, new)

    assert [change.kind for change in diff.changes] == [
        UnitChangeKind.UNCHANGED,
        UnitChangeKind.ADDED,
        UnitChangeKind.MOVED,
        UnitChangeKind.MODIFIED,
        UnitChangeKind.REMOVED,
    ]
    assert diff.changes[-1].old == gone
    assert 0.0 < diff.changes[3].similarity < 1.0
    assert diff.summary() == "+1 ~1 >1 =1 -1"

    refreshed = new[0].model_copy(update={"status": STALE})
    replaced = diff.with_new_units([refreshed])
    assert replaced.changes[0].new == refreshed
    assert replaced.changes[1:] == diff.changes[1:]


# ---------------------------------------------------------------------------
# Publication timeline, superseded/deleted, and as-of snapshots
# ---------------------------------------------------------------------------


def test_new_version_supersedes_and_deletes_and_history_stays_queryable(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    before = corpus.now()
    first = corpus.ingest()
    after_first = corpus.now()
    second = corpus.ingest(V2)

    assert corpus.lifecycle.snapshot(before).sources == ()
    assert corpus.effective_texts(after_first) == [
        "# Guide\nIntro text.",
        "## Install\nRun uv sync.",
        "## Usage\nRun the CLI.",
    ]
    assert corpus.effective_texts() == [
        "# Guide\nIntro text.",
        "## Usage\nRun the CLI with --database.",
        "## FAQ\nAsk in issues.",
    ]

    v1 = first.ingestion.document.id
    statuses = corpus.statuses()
    assert statuses[f"{v1}:# Guide\nIntro text."] is SUPERSEDED  # carried over unchanged
    assert statuses[f"{v1}:## Usage\nRun the CLI."] is SUPERSEDED  # modified
    assert statuses[f"{v1}:## Install\nRun uv sync."] is DELETED  # removed
    assert corpus.lifecycle.snapshot(include_retired=True).counts() == {
        "active": 3,
        "stale": 0,
        "superseded": 2,
        "deleted": 1,
    }

    # Historical as-of answers do not change when later versions arrive.
    assert set(corpus.statuses(after_first).values()) == {ACTIVE}

    # The cached status column mirrors the current projection.
    cached = corpus.cached()
    assert all(cached[item.id] is ACTIVE for item in second.units)
    assert [cached[item.id] for item in first.units] == [SUPERSEDED, DELETED, SUPERSEDED]
    assert all(item.status is ACTIVE for item in second.units)

    # Each head change is one source-level publication.
    with UnitOfWork(session_factory) as uow:
        timeline = load_timeline(uow, corpus.source_id)
    assert [item.document_id for item in timeline] == [v1, second.ingestion.document.id]


def test_reverting_to_old_content_republishes_the_old_version(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    second = corpus.ingest(V2)
    during_v2 = corpus.now()
    reverted = corpus.ingest(V1)
    again = corpus.ingest()

    assert reverted.reverted is True
    assert reverted.ingestion.created is False
    assert reverted.units_created is False
    assert reverted.previous_document_id == second.ingestion.document.id
    assert reverted.ingestion.document.id == first.ingestion.document.id
    assert reverted.diff is not None
    assert reverted.diff.summary() == "+1 ~1 >0 =1 -1"
    assert {item.id for item in reverted.units} == {item.id for item in first.units}
    assert again.diff is None and again.reverted is False

    assert corpus.effective_texts() == corpus.effective_texts(NOW + timedelta(minutes=2))
    assert "## FAQ\nAsk in issues." in corpus.effective_texts(during_v2)
    assert all(state.status is ACTIVE for state in corpus.lifecycle.snapshot().effective_units)
    assert corpus.cached()[by_text(second.units, "FAQ").id] is DELETED

    install = by_text(first.units, "Install")
    with UnitOfWork(session_factory) as uow:
        chain = uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, install.lineage_id)
        timeline = load_timeline(uow, corpus.source_id)
    assert [rev.payload["change"] for rev in chain] == ["added", "removed", "added"]
    assert chain[-1].payload["reverted"] is True
    assert [item.reverted for item in timeline] == [False, False, True]

    # The next new version diffs against the *head* (V1), not the newest document (V2).
    third = corpus.ingest(V3)
    assert third.previous_document_id == first.ingestion.document.id
    assert third.diff is not None
    assert third.diff.summary() == "+0 ~1 >0 =2 -0"


def test_revert_diff_brings_the_index_back_to_the_old_version(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    indexing = IndexingService(
        embedding_service=EmbeddingService(HashEmbeddingProvider(dimension=16)),
        vector_index=InMemoryVectorIndex(dimension=16),
    )
    first = corpus.ingest()
    indexing.index_units(first.units)
    second = corpus.ingest(V2)
    assert second.diff is not None
    indexing.apply_diff(second.diff)

    reverted = corpus.ingest(V1)
    assert reverted.diff is not None
    report = indexing.apply_diff(reverted.diff)

    index = indexing.vector_index
    assert len(index) == len(first.units)
    stored = index.get([str(item.id) for item in first.units])
    assert set(stored) == {str(item.id) for item in first.units}
    assert {record.payload["status"] for record in stored.values()} == {"active"}
    assert index.get([str(item.id) for item in second.units]) == {}
    assert len(report.embedded) == 2 and len(report.reused) == 1


def test_emptied_document_deletes_every_lineage(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    emptied = corpus.ingest("  \n")

    snapshot = corpus.lifecycle.snapshot(include_retired=True)
    assert len(snapshot.sources) == 1
    assert snapshot.sources[0].head is not None
    assert snapshot.sources[0].head.id == emptied.ingestion.document.id
    assert snapshot.effective_units == ()
    assert {state.status for state in snapshot.sources[0].retired} == {DELETED}
    assert {corpus.cached()[item.id] for item in first.units} == {DELETED}


# ---------------------------------------------------------------------------
# Stale marks
# ---------------------------------------------------------------------------


def test_stale_marks_survive_unchanged_reimports_and_clear_on_edits(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    install = by_text(first.units, "Install")
    intro = by_text(first.units, "Intro")
    before_mark = corpus.now()

    mark = corpus.lifecycle.mark_stale(install.id, reason="  uv 0.5 changed flags ", author="yk")
    corpus.lifecycle.mark_stale(intro.id, reason="check wording")

    assert mark.payload["reason"] == "uv 0.5 changed flags"
    assert mark.author == "yk"
    state = corpus.lifecycle.unit_state(install.id)
    assert state is not None
    assert state.status is STALE and state.stale_reason == "uv 0.5 changed flags"
    assert state.effective
    old_state = corpus.lifecycle.unit_state(install.id, before_mark)
    assert old_state is not None and old_state.status is ACTIVE
    assert corpus.cached()[install.id] is STALE

    # V3 edits Install but keeps Intro unchanged.
    third = corpus.ingest(V3)
    new_intro = by_text(third.units, "Intro")
    new_install = by_text(third.units, "Install")
    assert new_intro.status is STALE  # carried over without a revision
    assert new_install.status is ACTIVE  # the edit is a newer revision
    assert third.diff is not None
    assert {change.new.status for change in third.diff.changes if change.new} == {ACTIVE, STALE}
    snapshot = corpus.lifecycle.snapshot()
    assert snapshot.counts()["stale"] == 1

    corpus.lifecycle.refresh(new_intro.id, reason="reviewed")
    refreshed = corpus.lifecycle.unit_state(new_intro.id)
    assert refreshed is not None and refreshed.status is ACTIVE
    assert corpus.cached()[new_intro.id] is ACTIVE


def test_invalid_status_transitions_are_rejected(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    corpus.ingest(V2)
    old_usage = by_text(first.units, "Usage")
    current = corpus.lifecycle.snapshot().effective_units[0].unit

    with pytest.raises(LifecycleTransitionError, match="superseded"):
        corpus.lifecycle.mark_stale(old_usage.id, reason="old")
    with pytest.raises(LifecycleTransitionError, match="non-empty reason"):
        corpus.lifecycle.mark_stale(current.id, reason="  ")
    with pytest.raises(LifecycleTransitionError, match="already active"):
        corpus.lifecycle.refresh(current.id)
    corpus.lifecycle.mark_stale(current.id, reason="once")
    with pytest.raises(LifecycleTransitionError, match="already stale"):
        corpus.lifecycle.mark_stale(current.id, reason="twice")
    with pytest.raises(KnowledgeUnitNotFoundError):
        corpus.lifecycle.mark_stale(uuid4(), reason="missing")
    with pytest.raises(KnowledgeUnitNotFoundError):
        corpus.lifecycle.unit_state(uuid4())


def test_unpublished_units_have_no_status(tmp_path: Path, session_factory: SessionFactory) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()

    assert corpus.lifecycle.unit_state(first.units[0].id, NOW) is None
    # Simulate a split document that never became head (e.g. a crash mid-import).
    with UnitOfWork(session_factory) as uow:
        orphan = Document(
            id=uuid4(),
            source_id=corpus.source_id,
            locator=str(corpus.path),
            content_type=DocumentContentType.MARKDOWN,
            checksum=fingerprint_text("orphan"),
            ingested_at=corpus.clock(),
        )
        uow.documents.add(orphan)
        orphan_unit = unit(0, "Orphan.", document_id=orphan.id)
        uow.knowledge_units.add(orphan_unit)
        uow.commit()

    assert corpus.lifecycle.unit_state(orphan_unit.id) is None
    with pytest.raises(LifecycleTransitionError, match="unpublished"):
        corpus.lifecycle.mark_stale(orphan_unit.id, reason="never published")


def test_document_time_uses_first_publication(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    corpus.ingest(V2)
    corpus.ingest(V1)

    published = corpus.lifecycle.document_time(first.ingestion.document.id)
    with UnitOfWork(session_factory) as uow:
        timeline = load_timeline(uow, corpus.source_id)

    assert published == timeline[0].published_at
    with pytest.raises(DocumentNotFoundError):
        corpus.lifecycle.document_time(uuid4())


# ---------------------------------------------------------------------------
# Databases written before U10
# ---------------------------------------------------------------------------


def _downgrade_to_u09(engine: Engine) -> None:
    """Drop U10 publications and cached statuses, as a U09 database would look."""

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM revisions WHERE entity_type = 'source'"))
        connection.execute(text("UPDATE knowledge_units SET status = 'active'"))


def test_pre_u10_database_uses_implicit_timeline_and_reconciles(
    tmp_path: Path, engine: Engine, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    second = corpus.ingest(V2)
    _downgrade_to_u09(engine)

    # Reads infer the head from ingestion order.
    assert corpus.effective_texts()[1] == "## Usage\nRun the CLI with --database."
    assert set(corpus.cached().values()) == {ACTIVE}

    assert corpus.lifecycle.reconcile() == 3
    assert corpus.lifecycle.reconcile() == 0
    assert corpus.cached()[by_text(first.units, "Install").id] is DELETED

    # The next write materializes the inferred history before publishing.
    reverted = corpus.ingest(V1)
    assert reverted.reverted is True
    with UnitOfWork(session_factory) as uow:
        publications = uow.revisions.list_for(RevisionEntityType.SOURCE, corpus.source_id)
    assert [rev.payload.get("backfilled", False) for rev in publications] == [True, True, False]
    assert [rev.created_at for rev in publications[:2]] == [
        first.ingestion.document.ingested_at,
        second.ingestion.document.ingested_at,
    ]


def test_pre_u10_new_version_backfills_and_diffs_against_inferred_head(
    tmp_path: Path, engine: Engine, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    corpus.ingest()
    _downgrade_to_u09(engine)

    unchanged = corpus.ingest()
    assert unchanged.diff is None
    second = corpus.ingest(V2)

    assert second.diff is not None
    assert second.diff.summary() == "+1 ~1 >0 =1 -1"
    with UnitOfWork(session_factory) as uow:
        publications = uow.revisions.list_for(RevisionEntityType.SOURCE, corpus.source_id)
    assert [rev.payload.get("backfilled", False) for rev in publications] == [True, False]


def test_pre_u10_emptied_document_is_not_deleted_twice(
    tmp_path: Path, engine: Engine, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    corpus.ingest()
    corpus.ingest(" \n")
    _downgrade_to_u09(engine)

    again = corpus.ingest()

    assert again.diff is not None and again.diff.summary() == "+0 ~0 >0 =0 -3"
    with UnitOfWork(session_factory) as uow:
        deletes = [
            rev for rev in uow.revisions.list() if rev.change_type is RevisionChangeType.DELETE
        ]
    assert len(deletes) == 3
    assert corpus.lifecycle.snapshot().effective_units == ()


# ---------------------------------------------------------------------------
# Application and CLI
# ---------------------------------------------------------------------------


def test_library_list_and_history_report_statuses(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    corpus = Corpus(tmp_path, session_factory)
    first = corpus.ingest()
    corpus.ingest(V2)
    library = KnowledgeLibrary(session_factory)

    summaries = library.list_documents()
    heads = [summary.document.id for summary in summaries if summary.is_head]
    assert heads == [corpus.lifecycle.snapshot().sources[0].head.id]  # type: ignore[union-attr]
    old = next(
        summary for summary in summaries if summary.document.id == first.ingestion.document.id
    )
    assert set(old.statuses.values()) == {SUPERSEDED, DELETED}

    usage = by_text(first.units, "Usage")
    history = library.unit_history(usage.id)
    assert [history.statuses[version.id] for version in history.versions] == [SUPERSEDED, ACTIVE]
    assert library.unit_state(usage.id) is not None
    assert library.snapshot().counts()["active"] == 3


def _invoke(*arguments: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(arguments))
    return result.exit_code, result.stdout, result.stderr


def test_cli_asof_stale_refresh_and_reconcile(tmp_path: Path) -> None:
    database = str(tmp_path / "knowledge.db")
    guide = tmp_path / "guide.md"

    code, out, _ = _invoke("asof", "-d", database)
    assert code == 0 and "No knowledge was published" in out

    guide.write_text(V1, encoding="utf-8")
    _invoke("import", str(guide), "-d", database)
    code, out, _ = _invoke("asof", "-d", database)
    assert code == 0
    assert out.startswith("as of: now")
    first_document = out.split("head: document ")[1].split()[0]
    install_line = next(line for line in out.splitlines() if "Install" in line)
    install_id = install_line.split()[2]

    guide.write_text(V2, encoding="utf-8")
    _invoke("import", str(guide), "-d", database)
    guide.write_text(V1, encoding="utf-8")
    code, out, _ = _invoke("import", str(guide), "-d", database)
    assert code == 0 and out.startswith("reverted:")

    code, out, _ = _invoke("asof", "--document", first_document, "-d", database)
    assert code == 0
    assert "3 effective unit(s) across 1 source(s) | active=3" in out

    code, out, _ = _invoke("asof", "--all", "-d", database)
    assert "superseded" in out and "deleted" in out
    assert "active=3 stale=0 superseded=2 deleted=1" in out

    code, out, _ = _invoke("asof", "--at", "2000-01-01T00:00:00", "-d", database)
    assert code == 0 and "No knowledge was published" in out

    code, out, _ = _invoke("stale", install_id, "--reason", "flags changed", "-d", database)
    assert code == 0 and out.startswith(f"stale: unit {install_id}")
    code, out, _ = _invoke("show", install_id, "-d", database)
    assert "status: stale (flags changed)" in out
    assert f"lineage: {install_id}" in out
    code, out, _ = _invoke("show", install_id, "--at", "2000-01-01T00:00:00Z", "-d", database)
    assert "status: unpublished (as of 2000-01-01T00:00:00+00:00)" in out

    code, out, _ = _invoke("list", "-d", database)
    assert "version: current" in out and "version: historical" in out
    assert "status=stale" in out and "status=superseded" in out

    code, out, _ = _invoke("history", install_id, "-d", database)
    assert "(added, revert)" in out and "(stale)" in out and "status=stale" in out

    code, out, _ = _invoke("refresh", install_id, "-d", database)
    assert code == 0 and out.startswith(f"active: unit {install_id}")
    code, _, err = _invoke("refresh", install_id, "-d", database)
    assert code == 2 and "already active" in err

    code, out, _ = _invoke("reconcile", "-d", database)
    assert code == 0 and "0 unit(s) updated" in out


def test_cli_rejects_bad_time_points_and_unknown_ids(tmp_path: Path) -> None:
    database = str(tmp_path / "knowledge.db")

    code, _, err = _invoke("asof", "--at", "yesterday", "-d", database)
    assert code == 2 and "invalid --at" in err
    code, _, err = _invoke(
        "asof", "--at", "2026-10-30T00:00:00", "--document", str(uuid4()), "-d", database
    )
    assert code == 2 and "either --at or --document" in err
    code, _, err = _invoke("asof", "--document", str(uuid4()), "-d", database)
    assert code == 1 and "was not found" in err
    code, _, err = _invoke("stale", str(uuid4()), "--reason", "x", "-d", database)
    assert code == 1 and "was not found" in err


def test_main_returns_failure_exit_codes(tmp_path: Path) -> None:
    database = str(tmp_path / "knowledge.db")

    assert main(["list", "-d", database]) == 0
    assert main(["show", str(uuid4()), "-d", database]) == 1
    assert main(["asof", "--at", "nope", "-d", database]) == 2
