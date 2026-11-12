"""Root-Typer acceptance evidence for automatic evaluation and token budgets."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner, Result

from konwledge import cli
from konwledge.application import KnowledgeLibrary
from konwledge.context import ContextPacket, TokenContextPacket, TokenCounter
from konwledge.conversations import ConversationImportResult, ConversationReview
from konwledge.domain import EvaluationItem, EvaluationSet
from konwledge.evaluation import dump_evaluation_set
from konwledge.retrieval import RetrievalResult
from konwledge.storage import (
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

runner = CliRunner()
ANSWER_TOKEN = "azurequartz"
INITIAL_MARKDOWN = "# Operations\nThe launch code is azurequartz and the verified owner is Mira.\n"
PASSING_UPDATE = INITIAL_MARKDOWN + "This statement was reviewed again.\n"
FAILING_UPDATE = (
    "# Operations\nThe launch code was removed; contact the operations desk for verification.\n"
)


def invoke(*arguments: str) -> Result:
    """Invoke the registered root app via CliRunner."""

    return runner.invoke(cli.app, list(arguments))


def run_cli(*arguments: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the actual module entry point in an isolated process."""

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())
    return subprocess.run(
        [sys.executable, "-m", "konwledge", *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


@contextmanager
def open_library(database: Path) -> Iterator[KnowledgeLibrary]:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    try:
        yield KnowledgeLibrary(create_session_factory(engine))
    finally:
        engine.dispose()


@dataclass(frozen=True, slots=True)
class CliScenario:
    database: Path
    markdown: Path
    dataset_id: UUID
    source_id: UUID
    unit_id: UUID
    lineage_id: UUID


def create_scenario(tmp_path: Path, *, enable: bool = True) -> CliScenario:
    database = tmp_path / "knowledge.db"
    markdown = tmp_path / "operations.md"
    markdown.write_text(INITIAL_MARKDOWN, encoding="utf-8")
    imported = run_cli("import", str(markdown), "-d", str(database))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert imported.stderr == ""

    with open_library(database) as library:
        [summary] = library.list_documents()
        [unit] = summary.units
        source_id = summary.source.id

    dataset_id = uuid4()
    dataset = EvaluationSet(
        id=dataset_id,
        name="root CLI real lineage acceptance",
        version=1,
        created_at=datetime.now(UTC),
        items=(
            EvaluationItem(
                id="answer-from-real-file",
                query=ANSWER_TOKEN,
                expected_lineage_ids=frozenset({unit.lineage_id}),
            ),
        ),
    )
    dataset_path = tmp_path / "evaluation.json"
    dataset_path.write_text(dump_evaluation_set(dataset), encoding="utf-8")
    imported_dataset = run_cli(
        "evaluation", "import", str(dataset_path), "--json", "-d", str(database)
    )
    assert imported_dataset.returncode == 0
    assert json.loads(imported_dataset.stdout)["id"] == str(dataset_id)

    if enable:
        enabled = run_cli(
            "auto-evaluation",
            "enable",
            "--set-id",
            str(dataset_id),
            "--version",
            "1",
            "-d",
            str(database),
        )
        assert enabled.returncode == 0
        payload = json.loads(enabled.stdout)
        assert payload["enabled"] is True
        assert payload["dataset"]["id"] == str(dataset_id)

    return CliScenario(
        database=database,
        markdown=markdown,
        dataset_id=dataset_id,
        source_id=source_id,
        unit_id=unit.id,
        lineage_id=unit.lineage_id,
    )


def mark_stale_with_sdk(scenario: CliScenario) -> None:
    """Prepare a failing service state without consuming the CLI observation under test."""

    with open_library(scenario.database) as library:
        library.lifecycle.mark_stale(scenario.unit_id, reason="acceptance setup", author="test")
        assert library.automatic_evaluation.history()[0].status.value == "failed"


def create_conversation_candidate(scenario: CliScenario, tmp_path: Path) -> UUID:
    source = tmp_path / "conversation.json"
    content = json.dumps(
        {
            "schema_version": 1,
            "conversation_id": "auto-evaluation-root-cli",
            "version": 1,
            "messages": [
                {
                    "id": "message-1",
                    "role": "user",
                    "content": "Fact: The unrelated support window starts at noon.",
                }
            ],
        }
    )
    source.write_text(content, encoding="utf-8")
    imported = run_cli(
        "conversation", "import", str(source), "--json", "-d", str(scenario.database)
    )
    assert imported.returncode == 0
    result = ConversationImportResult.model_validate_json(imported.stdout)
    assert len(result.candidates) == 1
    return result.candidates[0].id


def test_disabled_root_import_preserves_old_outputs_and_context_json(tmp_path: Path) -> None:
    scenario = create_scenario(tmp_path, enable=False)

    context = run_cli("context", ANSWER_TOKEN, "--json", "-d", str(scenario.database))
    assert context.returncode == 0
    payload = json.loads(context.stdout)
    ContextPacket.model_validate_json(context.stdout)
    assert context.stderr == ""
    assert "max_tokens" not in payload
    assert "used_tokens" not in payload
    assert "token_encoding" not in payload
    assert "Automatic evaluation:" not in context.stderr

    candidate_id = create_conversation_candidate(scenario, tmp_path)
    assert isinstance(candidate_id, UUID)


def test_enabled_file_import_pass_then_failure_keeps_commit_and_normal_stdout(
    tmp_path: Path,
) -> None:
    scenario = create_scenario(tmp_path)
    scenario.markdown.write_text(PASSING_UPDATE, encoding="utf-8")

    passing = run_cli("import", str(scenario.markdown), "-d", str(scenario.database))

    assert passing.returncode == 0
    assert passing.stdout.startswith("updated:")
    assert "Imported 1 file(s):" in passing.stdout
    assert "Automatic evaluation: total=1" in passing.stderr
    assert "passed=1" in passing.stderr

    scenario.markdown.write_text(FAILING_UPDATE, encoding="utf-8")
    failing = run_cli("import", str(scenario.markdown), "-d", str(scenario.database))

    assert failing.returncode == 3
    assert failing.stdout.startswith("updated:")
    assert "Imported 1 file(s):" in failing.stdout
    assert "Automatic evaluation: total=1" in failing.stderr
    assert "failed=1" in failing.stderr
    assert "knowledge committed; evaluation did not pass" in failing.stderr

    committed = run_cli("search", "operations desk", "--json", "-d", str(scenario.database))
    retrieval = RetrievalResult.model_validate_json(committed.stdout)
    assert committed.returncode == 0 and committed.stderr == ""
    assert retrieval.hits
    assert retrieval.hits[0].unit.text == FAILING_UPDATE


def test_candidate_approve_failure_keeps_valid_review_json(tmp_path: Path) -> None:
    scenario = create_scenario(tmp_path)
    mark_stale_with_sdk(scenario)
    candidate_id = create_conversation_candidate(scenario, tmp_path)

    approved = run_cli(
        "candidate",
        "approve",
        str(candidate_id),
        "--reviewer",
        "acceptance-reviewer",
        "--reason",
        "verified",
        "--json",
        "-d",
        str(scenario.database),
    )

    assert approved.returncode == 3
    review = ConversationReview.model_validate_json(approved.stdout)
    assert review.candidate_id == candidate_id
    assert review.decision.value == "approved"
    assert "failed=1" in approved.stderr
    assert "knowledge committed; evaluation did not pass" in approved.stderr


def test_database_capture_failure_keeps_valid_capture_json(tmp_path: Path) -> None:
    scenario = create_scenario(tmp_path)
    mark_stale_with_sdk(scenario)
    target = tmp_path / "external.sqlite"
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY, email TEXT)")
        connection.commit()
    finally:
        connection.close()

    captured = run_cli(
        "database",
        "capture-sqlite",
        str(target),
        "--name",
        "Acceptance schema",
        "--json",
        "-d",
        str(scenario.database),
    )

    assert captured.returncode == 3
    payload = json.loads(captured.stdout)
    assert payload["created"] is True
    assert payload["dialect"] == "sqlite"
    assert "failed=1" in captured.stderr
    assert "knowledge committed; evaluation did not pass" in captured.stderr
    assert "capture failed" not in captured.stderr.lower()
    assert "metadata operation failed" not in captured.stderr.lower()


def test_stale_refresh_summaries_and_read_only_history_do_not_repeat_warning(
    tmp_path: Path,
) -> None:
    scenario = create_scenario(tmp_path)

    stale = run_cli(
        "stale",
        str(scenario.unit_id),
        "--reason",
        "source needs review",
        "-d",
        str(scenario.database),
    )
    assert stale.returncode == 3
    assert stale.stdout.startswith("stale: unit")
    assert "failed=1" in stale.stderr

    history = run_cli("auto-evaluation", "history", "-d", str(scenario.database))
    jobs = json.loads(history.stdout)
    assert history.returncode == 0 and history.stderr == ""
    assert [job["status"] for job in jobs] == ["failed"]
    assert jobs[0]["dataset"]["id"] == str(scenario.dataset_id)

    refreshed = run_cli(
        "refresh",
        str(scenario.unit_id),
        "--reason",
        "source reverified",
        "-d",
        str(scenario.database),
    )
    assert refreshed.returncode == 0
    assert refreshed.stdout.startswith("active: unit")
    assert "passed=1" in refreshed.stderr
    assert "knowledge committed; evaluation did not pass" not in refreshed.stderr

    refreshed_history = run_cli("auto-evaluation", "history", "-d", str(scenario.database))
    refreshed_jobs = json.loads(refreshed_history.stdout)
    assert refreshed_history.returncode == 0 and refreshed_history.stderr == ""
    assert [job["status"] for job in refreshed_jobs] == ["passed", "failed"]
    assert refreshed_jobs[1]["id"] == jobs[0]["id"]


class LocalBpeCounter:
    """Small deterministic local tokenizer used only to prove CLI argument forwarding."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def count(self, text: str, /) -> int:
        self.calls.append(text)
        symbols = list(text)
        for left, right in (("t", "h"), ("e", "r"), ("i", "n"), ("o", "n")):
            merged: list[str] = []
            index = 0
            while index < len(symbols):
                if index + 1 < len(symbols) and symbols[index : index + 2] == [left, right]:
                    merged.append(left + right)
                    index += 2
                else:
                    merged.append(symbols[index])
                    index += 1
            symbols = merged
        return len(symbols)


def test_context_max_tokens_runs_real_tiktoken_and_forwards_to_local_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = create_scenario(tmp_path, enable=False)

    actual = run_cli(
        "context",
        ANSWER_TOKEN,
        "--max-tokens",
        "100000",
        "--token-encoding",
        "cl100k_base",
        "--json",
        "-d",
        str(scenario.database),
    )
    assert actual.returncode == 0
    actual_packet = TokenContextPacket.model_validate_json(actual.stdout)
    assert actual_packet.token_encoding == "cl100k_base"
    assert actual_packet.used_tokens > 0

    counter = LocalBpeCounter()
    forwarded: list[tuple[int | None, str, TokenCounter | None]] = []

    def resolve_locally(
        *,
        max_tokens: int | None,
        token_encoding: str,
        token_counter: TokenCounter | None,
    ) -> TokenCounter | None:
        forwarded.append((max_tokens, token_encoding, token_counter))
        return counter

    monkeypatch.setattr(cli, "resolve_token_counter", resolve_locally)
    local = invoke(
        "context",
        ANSWER_TOKEN,
        "--max-tokens",
        "100000",
        "--token-encoding",
        "local-bpe-v1",
        "--json",
        "-d",
        str(scenario.database),
    )
    packet = TokenContextPacket.model_validate_json(local.stdout)
    assert local.exit_code == 0
    assert forwarded == [(100000, "local-bpe-v1", None)]
    assert counter.calls
    assert packet.used_tokens == counter.count(packet.context_text)


def test_invalid_token_encoding_fails_before_database_creation(tmp_path: Path) -> None:
    database = tmp_path / "invalid-encoding" / "knowledge.db"

    result = run_cli(
        "context",
        ANSWER_TOKEN,
        "--max-tokens",
        "10",
        "--token-encoding",
        "definitely-not-a-tiktoken-encoding",
        "--json",
        "-d",
        str(database),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unknown tiktoken encoding" in result.stderr
    assert not database.exists() and not database.parent.exists()


def test_missing_token_dependency_fails_before_database_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "missing-dependency" / "knowledge.db"

    def missing_import(name: str, package: str | None = None) -> ModuleType:
        assert name == "tiktoken"
        assert package is None
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("konwledge.context.tokens.importlib.import_module", missing_import)
    result = invoke(
        "context",
        ANSWER_TOKEN,
        "--max-tokens",
        "10",
        "--json",
        "-d",
        str(database),
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "optional 'tiktoken' dependency" in result.stderr
    assert not database.exists() and not database.parent.exists()
