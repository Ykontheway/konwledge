"""V1: demo corpus, deterministic demo build, snapshot export, and golden files."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import jsonschema
import pytest
from typer.testing import CliRunner

from konwledge.application import KnowledgeLibrary
from konwledge.cli import app
from konwledge.demo import (
    DEMO_STEPS,
    DemoBuild,
    DemoWorkspaceError,
    ScriptedClock,
    SequentialIds,
    build_demo,
    corpus_text,
    export_demo,
)
from konwledge.demo.builder import DEMO_NAMESPACE, MARKER
from konwledge.demo.scenario import FAQ, GUIDE, DemoStep, ImportFile
from konwledge.domain import KnowledgeUnitStatus
from konwledge.ingestion import normalize_text
from konwledge.lifecycle import UnitChangeKind
from konwledge.snapshot import (
    SCHEMA_VERSION,
    LibrarySnapshot,
    SourceView,
    dump_schema,
    dump_snapshot,
    export_snapshot,
    snapshot_json_schema,
)
from konwledge.storage import (
    SessionFactory,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

REPO = Path(__file__).resolve().parents[1]
GOLDEN_SNAPSHOT = REPO / "demo" / "snapshot.json"
GOLDEN_SCHEMA = REPO / "demo" / "snapshot.schema.json"

runner = CliRunner()


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> DemoBuild:
    return build_demo(tmp_path_factory.mktemp("demo") / "workspace")


@pytest.fixture(scope="module")
def snapshot(demo: DemoBuild) -> LibrarySnapshot:
    return export_demo(demo.workspace)


def _source(snapshot: LibrarySnapshot, uri: str) -> SourceView:
    [source] = [source for source in snapshot.sources if source.uri == uri]
    return source


def _lineage_by_label(snapshot: LibrarySnapshot, source: SourceView, label: str) -> list[UUID]:
    return [
        lineage.id
        for lineage in snapshot.lineages
        if lineage.source_id == source.id and lineage.label == label
    ]


# ---------------------------------------------------------------------------
# Golden files and determinism (V1 acceptance)
# ---------------------------------------------------------------------------


def test_committed_snapshot_matches_a_fresh_export(snapshot: LibrarySnapshot) -> None:
    expected = GOLDEN_SNAPSHOT.read_text(encoding="utf-8")
    assert dump_snapshot(snapshot) == expected, (
        "demo/snapshot.json is stale; run `knowledge demo build --force && knowledge demo export`"
    )


def test_committed_schema_matches_the_models() -> None:
    assert dump_schema() == GOLDEN_SCHEMA.read_text(encoding="utf-8")


def test_two_builds_in_different_directories_export_identical_bytes(
    tmp_path: Path, snapshot: LibrarySnapshot
) -> None:
    other = build_demo(tmp_path / "elsewhere" / "nested")

    first = dump_snapshot(snapshot).encode("utf-8")
    second = dump_snapshot(export_demo(other.workspace)).encode("utf-8")
    again = dump_snapshot(export_demo(other.workspace)).encode("utf-8")

    assert first == second == again


def test_snapshot_does_not_leak_machine_paths(demo: DemoBuild, snapshot: LibrarySnapshot) -> None:
    text = dump_snapshot(snapshot)

    assert str(demo.workspace) not in text
    assert [source.uri for source in snapshot.sources] == [GUIDE, FAQ]


def test_snapshot_validates_against_the_published_json_schema(
    snapshot: LibrarySnapshot,
) -> None:
    schema = json.loads(dump_schema())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())

    validator.validate(json.loads(dump_snapshot(snapshot)))
    assert schema["$schema"].endswith("2020-12/schema")
    assert snapshot_json_schema()["title"] == "LibrarySnapshot"


def test_snapshot_round_trips_through_the_model(snapshot: LibrarySnapshot) -> None:
    restored = LibrarySnapshot.model_validate_json(dump_snapshot(snapshot))

    assert restored == snapshot
    assert restored.schema_version == SCHEMA_VERSION == 1


def test_dump_is_canonical_utf8_with_trailing_newline(snapshot: LibrarySnapshot) -> None:
    text = dump_snapshot(snapshot)

    assert text.endswith("}\n")
    assert "\r" not in text
    assert "服务部署手册" in text  # not \u-escaped


# ---------------------------------------------------------------------------
# Story coverage
# ---------------------------------------------------------------------------


def test_guide_v1_to_v2_covers_all_five_diff_kinds(snapshot: LibrarySnapshot) -> None:
    guide = _source(snapshot, GUIDE)
    first, second = guide.diffs

    assert (first.from_version, first.to_version) == (1, 2)
    assert first.counts == {"added": 2, "modified": 1, "moved": 1, "unchanged": 5, "removed": 1}
    assert {change.kind for change in first.changes} == set(UnitChangeKind)
    assert first.summary == "+2 ~1 >1 =5 -1"
    assert (second.from_version, second.to_version) == (2, 3)
    assert second.counts["modified"] == 2
    assert all(count >= 1 for count in snapshot.stats.diff_kinds.values())


def test_diff_embedding_work_matches_the_real_incremental_index(
    demo: DemoBuild, snapshot: LibrarySnapshot
) -> None:
    guide = _source(snapshot, GUIDE)
    reported = [
        (result.embedded, result.reused, result.deleted)
        for result in demo.steps
        if result.detail.startswith("changes=")
    ]

    assert reported == [
        (diff.embedding.embedded, diff.embedding.reused, diff.embedding.deleted)
        for diff in guide.diffs
    ]
    assert reported[0] == (3, 6, 8)
    assert snapshot.stats.vectors == demo.vectors == 12


def test_document_content_and_unit_ranges_are_exact(snapshot: LibrarySnapshot) -> None:
    corpus = {
        (GUIDE, 1): "deployment-guide.v1.md",
        (GUIDE, 2): "deployment-guide.v2.md",
        (GUIDE, 3): "deployment-guide.v3.md",
        (FAQ, 1): "oncall-faq.v1.md",
    }
    for source in snapshot.sources:
        for document in source.documents:
            assert document.version is not None
            expected = normalize_text(corpus_text(corpus[(source.uri, document.version)]))
            assert document.content == expected
            for unit in document.units:
                assert expected[unit.start_char : unit.end_char] == unit.text


def test_final_statuses_cover_all_four_states(snapshot: LibrarySnapshot) -> None:
    assert snapshot.stats.statuses == {"active": 11, "stale": 1, "superseded": 16, "deleted": 1}
    faq = _source(snapshot, FAQ)
    [answer] = [unit for unit in faq.documents[0].units if unit.heading == "服务端口是多少？"]

    assert answer.status is KnowledgeUnitStatus.STALE
    assert answer.stale_reason is not None and "8080" in answer.stale_reason


def test_lineages_tell_the_story(snapshot: LibrarySnapshot) -> None:
    guide = _source(snapshot, GUIDE)
    by_id = {lineage.id: lineage for lineage in snapshot.lineages}
    [port] = _lineage_by_label(snapshot, guide, "服务端口")
    [health] = _lineage_by_label(snapshot, guide, "健康检查")
    [rollback] = _lineage_by_label(snapshot, guide, "回滚脚本")
    [alerting] = _lineage_by_label(snapshot, guide, "监控告警")

    assert [revision.change for revision in by_id[port].revisions] == [
        "added",
        "modified",
        "modified",
    ]
    assert len(by_id[port].unit_ids) == 3
    assert [revision.change for revision in by_id[health].revisions] == [
        "added",
        "moved",
        "stale",
        "refreshed",
    ]
    assert by_id[rollback].status is KnowledgeUnitStatus.DELETED
    assert by_id[alerting].status is KnowledgeUnitStatus.ACTIVE  # v3 cleared the stale mark
    assert by_id[health].revisions[2].reason == "探针路径待确认"


def test_timepoints_reproduce_as_of_views(demo: DemoBuild, snapshot: LibrarySnapshot) -> None:
    guide = _source(snapshot, GUIDE)
    v1, v2, v3 = guide.documents
    first, *_rest = snapshot.timepoints

    assert [point.at for point in snapshot.timepoints] == sorted(
        {event.at for event in snapshot.events}
    )
    assert first.effective_unit_ids == [unit.id for unit in v1.units]
    assert first.head_document_ids == [v1.id]

    at_v2 = next(
        point for point in snapshot.timepoints if point.at == guide.timeline[1].published_at
    )
    assert all(at_v2.statuses[str(unit.id)] is not KnowledgeUnitStatus.ACTIVE for unit in v1.units)
    assert {unit.id for unit in v2.units} <= set(at_v2.effective_unit_ids)
    assert all(str(unit.id) not in at_v2.statuses for unit in v3.units)

    two_stale = [point for point in snapshot.timepoints if point.counts["stale"] == 2]
    assert len(two_stale) == 1

    library = KnowledgeLibrary(_session_factory(demo.database))
    for point in snapshot.timepoints:
        expected = library.snapshot(point.at)
        assert point.effective_unit_ids == [state.unit.id for state in expected.effective_units]


def test_story_steps_line_up_with_events(snapshot: LibrarySnapshot) -> None:
    event_times = {event.at for event in snapshot.events}

    assert [step.title for step in snapshot.story] == [step.title for step in DEMO_STEPS]
    assert all(step.at in event_times for step in snapshot.story)
    assert snapshot.as_of == DEMO_STEPS[-1].at
    kinds = [event.kind for event in snapshot.events]
    assert kinds.count("published") == 4
    assert kinds.count("stale") == 3
    assert kinds.count("refreshed") == 1


# ---------------------------------------------------------------------------
# Builder safety and determinism helpers
# ---------------------------------------------------------------------------


def test_sequential_ids_and_scripted_clock_are_deterministic() -> None:
    ids = SequentialIds()
    assert [ids(), ids()] == [uuid5(DEMO_NAMESPACE, "1"), uuid5(DEMO_NAMESPACE, "2")]

    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = ScriptedClock(start)
    assert clock() == start
    clock.set(start + timedelta(hours=1))
    assert clock() == start + timedelta(hours=1)
    with pytest.raises(ValueError, match="back in time"):
        clock.set(start)


def test_build_refuses_foreign_or_existing_workspaces(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(DemoWorkspaceError, match="not a demo workspace"):
        build_demo(foreign, force=True)
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "keep me"

    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(DemoWorkspaceError, match="not a directory"):
        build_demo(a_file)

    workspace = tmp_path / "demo"
    build_demo(workspace)
    with pytest.raises(DemoWorkspaceError, match="--force"):
        build_demo(workspace)

    (workspace / "leftover").mkdir()
    (workspace / "leftover.txt").write_text("old", encoding="utf-8")
    rebuilt = build_demo(workspace, force=True)
    assert not (workspace / "leftover").exists()
    assert not (workspace / "leftover.txt").exists()
    assert (workspace / MARKER).is_file()
    assert rebuilt.vectors == 12


def test_build_rejects_an_empty_scenario(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one step"):
        build_demo(tmp_path / "demo", steps=())


def test_export_requires_a_built_workspace(tmp_path: Path) -> None:
    with pytest.raises(DemoWorkspaceError, match="demo build"):
        export_demo(tmp_path / "missing")


def test_reimporting_identical_content_is_reported_as_unchanged(tmp_path: Path) -> None:
    step = DEMO_STEPS[0]
    again = DemoStep(
        at=step.at + timedelta(minutes=1),
        title="再次导入",
        description="内容相同",
        action=ImportFile(GUIDE, "deployment-guide.v1.md"),
    )

    build = build_demo(tmp_path / "demo", steps=(step, again))

    assert build.steps[1].detail == "unchanged"
    assert build.vectors == 8


# ---------------------------------------------------------------------------
# Generic exporter on an ordinary library
# ---------------------------------------------------------------------------


def _session_factory(database: Path) -> SessionFactory:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    return create_session_factory(engine)


@pytest.fixture
def clock() -> Iterator[ScriptedClock]:
    yield ScriptedClock(datetime(2026, 10, 1, tzinfo=UTC))


def test_empty_library_exports_an_empty_snapshot(tmp_path: Path) -> None:
    snapshot = export_snapshot(_session_factory(tmp_path / "k.db"))

    assert snapshot.as_of is None
    assert snapshot.sources == []
    assert snapshot.timepoints == []
    assert snapshot.stats.vectors == 0


def test_exporter_handles_reverts_text_files_and_relative_uris(
    tmp_path: Path, clock: ScriptedClock
) -> None:
    sessions = _session_factory(tmp_path / "k.db")
    library = KnowledgeLibrary(sessions, id_factory=SequentialIds(), clock=clock)
    folder = tmp_path / "notes" / "team"
    folder.mkdir(parents=True)
    note = folder / "note.md"
    plain = tmp_path / "notes" / "plain.txt"
    plain.write_text("first paragraph\n\nsecond paragraph\n\n   ", encoding="utf-8")

    note.write_text("# Note\n\nalpha\n", encoding="utf-8")
    library.import_path(note)
    library.import_path(plain)
    clock.set(datetime(2026, 10, 2, tzinfo=UTC))
    note.write_text("# Note\n\nbeta\n", encoding="utf-8")
    library.import_path(note)
    clock.set(datetime(2026, 10, 3, tzinfo=UTC))
    note.write_text("# Note\n\nalpha\n", encoding="utf-8")
    library.import_path(note)

    snapshot = export_snapshot(sessions, title="notes")
    note_view = _source(snapshot, "team/note.md")
    plain_view = _source(snapshot, "plain.txt")

    assert [publication.version for publication in note_view.timeline] == [1, 2, 1]
    assert [diff.reverted for diff in note_view.diffs] == [False, True]
    assert note_view.diffs[1].to_version == 1
    assert note_view.head_document_id == note_view.documents[0].id
    assert [event.kind for event in snapshot.events if event.source_id == note_view.id] == [
        "published",
        "published",
        "reverted",
    ]
    # Trailing blank lines of a text file are not covered by any unit.
    assert plain_view.documents[0].content is None
    assert note_view.documents[0].content == "# Note\n\nalpha\n"
    assert export_snapshot(sessions, root=tmp_path).sources[0].uri == "notes/plain.txt"


def test_library_uses_injected_ids_and_clock_for_every_layer(
    tmp_path: Path, clock: ScriptedClock
) -> None:
    sessions = _session_factory(tmp_path / "k.db")
    library = KnowledgeLibrary(sessions, id_factory=SequentialIds(), clock=clock)
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nalpha\n", encoding="utf-8")

    [imported] = library.import_path(note).imported

    namespace_ids = {uuid5(DEMO_NAMESPACE, str(number)) for number in range(1, 10)}
    assert imported.result.ingestion.document.id in namespace_ids
    assert all(unit.id in namespace_ids for unit in imported.result.units)
    assert imported.result.ingestion.document.ingested_at == clock()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_demo_build_export_and_check(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    output = tmp_path / "out"

    built = runner.invoke(app, ["demo", "build", "--workspace", str(workspace)])
    assert built.exit_code == 0, built.output
    assert "embedded=3 reused=6 deleted=8" in built.stdout
    assert "12 vectors" in built.stdout

    refused = runner.invoke(app, ["demo", "build", "--workspace", str(workspace)])
    assert refused.exit_code == 2

    missing = runner.invoke(
        app, ["demo", "export", "-w", str(workspace), "-o", str(output), "--check"]
    )
    assert missing.exit_code == 1
    assert "out of date" in missing.output

    exported = runner.invoke(app, ["demo", "export", "-w", str(workspace), "-o", str(output)])
    assert exported.exit_code == 0, exported.output
    assert (output / "snapshot.json").read_text(encoding="utf-8") == GOLDEN_SNAPSHOT.read_text(
        encoding="utf-8"
    )
    assert "schema v1" in exported.stdout

    checked = runner.invoke(
        app, ["demo", "export", "-w", str(workspace), "-o", str(output), "--check"]
    )
    assert checked.exit_code == 0
    assert "up to date" in checked.stdout

    (output / "snapshot.schema.json").write_text("{}\n", encoding="utf-8")
    drifted = runner.invoke(
        app, ["demo", "export", "-w", str(workspace), "-o", str(output), "--check"]
    )
    assert drifted.exit_code == 1
    assert "snapshot.schema.json" in drifted.output


def test_cli_demo_export_without_build_fails_clearly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "export", "-w", str(tmp_path / "none")])

    assert result.exit_code == 2
    assert "knowledge demo build" in result.output


def test_cli_import_reports_republished_versions(tmp_path: Path) -> None:
    database = tmp_path / "k.db"
    note = tmp_path / "note.md"
    for text in ("# N\n\na\n", "# N\n\nb\n", "# N\n\na\n"):
        note.write_text(text, encoding="utf-8")
        result = runner.invoke(app, ["import", str(note), "--database", str(database)])
        assert result.exit_code == 0

    assert "1 file(s) republished an earlier version" in result.stdout
