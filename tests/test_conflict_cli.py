"""End-to-end acceptance tests for the conflict CLI."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner, Result

from konwledge.application import KnowledgeLibrary
from konwledge.cli import app
from konwledge.domain import Conflict, ConflictStatus
from konwledge.storage import (
    SessionFactory,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

BASE = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
LEFT_QUOTE = "order-service listens on port 8000"
RIGHT_QUOTE = "order-service listens on port 8080"


@dataclass(slots=True)
class ConflictCliCorpus:
    """A real two-file library opened through the CLI's normal application API."""

    library: KnowledgeLibrary
    session_factory: SessionFactory
    database: Path
    before_v2: datetime
    now: list[datetime]
    runner: CliRunner

    def invoke(self, *arguments: str) -> Result:
        return self.runner.invoke(app, list(arguments))

    def conflict_args(self, command: str, *arguments: str) -> tuple[str, ...]:
        return ("conflicts", command, *arguments, "-d", str(self.database))


@pytest.fixture
def conflict_cli_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ConflictCliCorpus]:
    """Import v1/v2 files for real while injecting a deterministic application clock."""

    database = tmp_path / "knowledge.db"
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    now = [BASE]
    library = KnowledgeLibrary(session_factory, clock=lambda: now[0])
    monkeypatch.setattr("konwledge.cli._open_library", lambda _database: library)
    runner = CliRunner()

    left = tmp_path / "left.md"
    right = tmp_path / "right.md"
    left.write_text(LEFT_QUOTE, encoding="utf-8")
    right.write_text(LEFT_QUOTE, encoding="utf-8")

    left_import = runner.invoke(app, ["import", str(left), "-d", str(database)])
    assert left_import.exit_code == 0
    now[0] = BASE + timedelta(minutes=1)
    right_v1_import = runner.invoke(app, ["import", str(right), "-d", str(database)])
    assert right_v1_import.exit_code == 0

    before_v2 = BASE + timedelta(minutes=2)
    now[0] = BASE + timedelta(minutes=3)
    right.write_text(RIGHT_QUOTE, encoding="utf-8")
    right_v2_import = runner.invoke(app, ["import", str(right), "-d", str(database)])
    assert right_v2_import.exit_code == 0
    assert "updated:" in right_v2_import.stdout
    now[0] = BASE + timedelta(minutes=4)

    try:
        yield ConflictCliCorpus(
            library=library,
            session_factory=session_factory,
            database=database,
            before_v2=before_v2,
            now=now,
            runner=runner,
        )
    finally:
        engine.dispose()


def assert_scan_counts(output: str, *, candidates: int, created: int, existing: int) -> None:
    """Require the stable machine-readable count labels without fixing prose around them."""

    for label, count in {
        "candidates": candidates,
        "created": created,
        "existing": existing,
    }.items():
        assert re.search(rf"\b{label}\s*(?:=|:)\s*{count}\b", output)


def apply_one_conflict(corpus: ConflictCliCorpus) -> Conflict:
    result = corpus.invoke(*corpus.conflict_args("scan", "--apply"))
    assert result.exit_code == 0, result.output
    assert_scan_counts(result.stdout, candidates=1, created=1, existing=0)
    [conflict] = corpus.library.conflicts.list()
    return conflict


def test_scan_preview_apply_and_idempotent_existing_counts(
    conflict_cli_corpus: ConflictCliCorpus,
) -> None:
    corpus = conflict_cli_corpus
    [candidate] = corpus.library.conflicts.scan().candidates

    preview = corpus.invoke(*corpus.conflict_args("scan"))

    assert preview.exit_code == 0, preview.output
    assert_scan_counts(preview.stdout, candidates=1, created=0, existing=0)
    assert "candidate" in preview.stdout.lower()
    assert candidate.key in preview.stdout
    assert candidate.left.source_uri in preview.stdout
    assert candidate.right.source_uri in preview.stdout
    assert candidate.left.value in preview.stdout
    assert candidate.right.value in preview.stdout
    assert corpus.library.conflicts.list() == ()

    conflict = apply_one_conflict(corpus)
    assert conflict.key == candidate.key
    assert conflict.detected_at == corpus.now[0]
    assert conflict.status is ConflictStatus.PENDING

    repeated = corpus.invoke(*corpus.conflict_args("scan", "--apply"))
    assert repeated.exit_code == 0, repeated.output
    assert_scan_counts(repeated.stdout, candidates=1, created=0, existing=1)
    assert corpus.library.conflicts.list() == (conflict,)


def test_historical_scan_before_v2_is_preview_only_and_has_no_candidate(
    conflict_cli_corpus: ConflictCliCorpus,
) -> None:
    corpus = conflict_cli_corpus
    result = corpus.invoke(*corpus.conflict_args("scan", "--at", corpus.before_v2.isoformat()))

    assert result.exit_code == 0, result.output
    assert_scan_counts(result.stdout, candidates=0, created=0, existing=0)
    assert corpus.library.conflicts.list() == ()


def test_list_show_review_history_and_persistence(
    conflict_cli_corpus: ConflictCliCorpus,
) -> None:
    corpus = conflict_cli_corpus
    conflict = apply_one_conflict(corpus)

    listed_all = corpus.invoke(*corpus.conflict_args("list"))
    listed_pending = corpus.invoke(*corpus.conflict_args("list", "--status", "pending"))
    listed_confirmed = corpus.invoke(*corpus.conflict_args("list", "--status", "confirmed"))
    assert listed_all.exit_code == listed_pending.exit_code == listed_confirmed.exit_code == 0
    assert str(conflict.id) in listed_all.stdout
    assert str(conflict.id) in listed_pending.stdout
    assert "pending" in listed_pending.stdout
    assert str(conflict.id) not in listed_confirmed.stdout

    shown = corpus.invoke(*corpus.conflict_args("show", str(conflict.id)))
    assert shown.exit_code == 0, shown.output
    assert shown.stdout == conflict.model_dump_json(indent=2) + "\n"
    shown_payload = json.loads(shown.stdout)
    evidence = (shown_payload["left"], shown_payload["right"])
    assert {item["quote"] for item in evidence} == {LEFT_QUOTE, RIGHT_QUOTE}
    assert {(item["start_char"], item["end_char"]) for item in evidence} == {(0, 34)}
    assert {item["value"] for item in evidence} == {"8000", "8080"}

    corpus.now[0] += timedelta(minutes=1)
    reviewed_result = corpus.invoke(
        *corpus.conflict_args(
            "review",
            str(conflict.id),
            "--status",
            "confirmed",
            "--reason",
            "  Production sources disagree  ",
            "--reviewer",
            "  alice@example.com  ",
        )
    )
    assert reviewed_result.exit_code == 0, reviewed_result.output
    assert str(conflict.id) in reviewed_result.stdout
    assert "confirmed" in reviewed_result.stdout

    reviewed = corpus.library.conflicts.get(conflict.id)
    assert reviewed.status is ConflictStatus.CONFIRMED
    assert reviewed.review_reason == "Production sources disagree"
    assert reviewed.reviewer == "alice@example.com"
    assert reviewed.reviewed_at == corpus.now[0]

    for status in ConflictStatus:
        filtered = corpus.invoke(*corpus.conflict_args("list", "--status", status.value))
        assert filtered.exit_code == 0, filtered.output
        if status is ConflictStatus.CONFIRMED:
            assert str(conflict.id) in filtered.stdout
        else:
            assert str(conflict.id) not in filtered.stdout
    assert str(conflict.id) in corpus.invoke(*corpus.conflict_args("list")).stdout

    history_result = corpus.invoke(*corpus.conflict_args("history", str(conflict.id)))
    assert history_result.exit_code == 0, history_result.output
    assert str(conflict.id) in history_result.stdout
    assert "detected" in history_result.stdout
    assert "reviewed" in history_result.stdout
    assert "pending -> confirmed" in history_result.stdout
    assert "Production sources disagree" in history_result.stdout
    assert "alice@example.com" in history_result.stdout
    assert conflict.key in history_result.stdout

    history = corpus.library.conflicts.history(conflict.id)
    assert [item.revision for item in history] == [1, 2]
    assert history[0].payload == {
        "change": "detected",
        "key": conflict.key,
        "rule": conflict.rule,
        "status": "pending",
        "evidence": {
            "left": conflict.left.model_dump(mode="json"),
            "right": conflict.right.model_dump(mode="json"),
        },
    }
    assert history[1].payload == {
        "change": "reviewed",
        "from_status": "pending",
        "to_status": "confirmed",
        "reason": "Production sources disagree",
        "reviewer": "alice@example.com",
    }

    reopened = KnowledgeLibrary(corpus.session_factory, clock=lambda: corpus.now[0])
    assert reopened.conflicts.get(conflict.id) == reviewed
    assert reopened.conflicts.history(conflict.id) == history


def test_cli_domain_and_parameter_errors_have_stable_exit_codes(
    conflict_cli_corpus: ConflictCliCorpus,
) -> None:
    corpus = conflict_cli_corpus
    missing = uuid4()

    missing_commands = (
        corpus.conflict_args("show", str(missing)),
        corpus.conflict_args("history", str(missing)),
        corpus.conflict_args(
            "review",
            str(missing),
            "--status",
            "confirmed",
            "--reason",
            "investigated",
        ),
    )
    for arguments in missing_commands:
        result = corpus.invoke(*arguments)
        assert result.exit_code == 1, result.output
        assert "was not found" in result.output

    conflict = apply_one_conflict(corpus)
    blank = corpus.invoke(
        *corpus.conflict_args(
            "review",
            str(conflict.id),
            "--status",
            "confirmed",
            "--reason",
            "   ",
        )
    )
    same_status = corpus.invoke(
        *corpus.conflict_args(
            "review",
            str(conflict.id),
            "--status",
            "pending",
            "--reason",
            "duplicate",
        )
    )
    historical_apply = corpus.invoke(
        *corpus.conflict_args(
            "scan",
            "--apply",
            "--at",
            corpus.before_v2.isoformat(),
        )
    )
    invalid_date = corpus.invoke(*corpus.conflict_args("scan", "--at", "not-an-iso-date"))

    assert blank.exit_code == 2
    assert "non-empty reason" in blank.output
    assert same_status.exit_code == 2
    assert "already pending" in same_status.output
    assert historical_apply.exit_code == 2
    assert "historical" in historical_apply.output
    assert invalid_date.exit_code == 2
    assert "invalid --at" in invalid_date.output
    assert corpus.library.conflicts.get(conflict.id).status is ConflictStatus.PENDING
    assert len(corpus.library.conflicts.history(conflict.id)) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ("conflicts", "list", "--status", "unknown"),
        (
            "conflicts",
            "review",
            "00000000-0000-0000-0000-000000000000",
            "--status",
            "unknown",
            "--reason",
            "x",
        ),
        ("conflicts", "show", "not-a-uuid"),
    ],
)
def test_click_rejects_unknown_status_and_invalid_uuid(
    conflict_cli_corpus: ConflictCliCorpus, arguments: tuple[str, ...]
) -> None:
    corpus = conflict_cli_corpus
    result = corpus.invoke(*arguments, "-d", str(corpus.database))

    assert result.exit_code == 2
    assert "Invalid value" in result.output
