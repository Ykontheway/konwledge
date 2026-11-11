"""Human-reviewed conversation candidate CLI acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner, Result

from konwledge.cli import app
from konwledge.context import ContextPacket
from konwledge.conversations import ConversationCandidate, ConversationReview
from konwledge.retrieval import RetrievalResult

runner = CliRunner()


def invoke(*arguments: str) -> Result:
    return runner.invoke(app, list(arguments))


def import_candidates(tmp_path: Path) -> tuple[Path, Path, list[ConversationCandidate]]:
    source = tmp_path / "conversation.json"
    database = tmp_path / "knowledge.db"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "conversation_id": "candidate-cli",
                "version": 1,
                "messages": [
                    {
                        "id": "user-fact",
                        "role": "user",
                        "content": "事实：候选事实 unique-approved-token",
                    },
                    {
                        "id": "assistant-decision",
                        "role": "assistant",
                        "content": "决策：候选决策 unique-rejected-token",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    imported = invoke("conversation", "import", str(source), "--json", "-d", str(database))
    assert imported.exit_code == 0, imported.output
    candidates = [
        ConversationCandidate.model_validate(item)
        for item in json.loads(invoke("candidate", "list", "--json", "-d", str(database)).stdout)
    ]
    assert len(candidates) == 2
    return source, database, candidates


def test_pending_import_is_not_searchable_or_context_and_creates_no_knowledge_rows(
    tmp_path: Path,
) -> None:
    _, database, candidates = import_candidates(tmp_path)

    search = invoke("search", "unique-approved-token", "--json", "-d", str(database))
    context = invoke("context", "unique-approved-token", "--json", "-d", str(database))
    documents = invoke("list", "-d", str(database))
    pending = invoke("candidate", "list", "--json", "-d", str(database))

    assert search.exit_code == context.exit_code == documents.exit_code == pending.exit_code == 0
    assert RetrievalResult.model_validate_json(search.stdout).hits == ()
    assert ContextPacket.model_validate_json(context.stdout).blocks == ()
    assert "Knowledge library is empty" in documents.stdout
    assert [item["id"] for item in json.loads(pending.stdout)] == [
        str(item.id) for item in candidates
    ]


def test_approve_and_reject_have_terminal_audit_and_reimport_preserves_decisions(
    tmp_path: Path,
) -> None:
    source, database, candidates = import_candidates(tmp_path)
    approved, rejected = candidates

    approval = invoke(
        "candidate",
        "approve",
        str(approved.id),
        "--reviewer",
        "alice@example.com",
        "--reason",
        "verified",
        "--json",
        "-d",
        str(database),
    )
    rejection = invoke(
        "candidate",
        "reject",
        str(rejected.id),
        "--reviewer",
        "bob@example.com",
        "--reason",
        "not durable",
        "--json",
        "-d",
        str(database),
    )
    assert approval.exit_code == rejection.exit_code == 0
    approval_review = ConversationReview.model_validate_json(approval.stdout)
    rejection_review = ConversationReview.model_validate_json(rejection.stdout)
    assert approval_review.decision.value == "approved"
    assert rejection_review.decision.value == "rejected"

    approved_search = invoke("search", "unique-approved-token", "--json", "-d", str(database))
    rejected_search = invoke("search", "unique-rejected-token", "--json", "-d", str(database))
    approved_context = invoke("context", "unique-approved-token", "--json", "-d", str(database))
    assert len(RetrievalResult.model_validate_json(approved_search.stdout).hits) == 1
    assert RetrievalResult.model_validate_json(rejected_search.stdout).hits == ()
    assert len(ContextPacket.model_validate_json(approved_context.stdout).blocks) == 1

    repeated = invoke("conversation", "import", str(source), "-d", str(database))
    all_candidates = invoke("candidate", "list", "--status", "all", "--json", "-d", str(database))
    assert repeated.exit_code == 0 and repeated.stdout.startswith("existing:")
    assert {item["status"] for item in json.loads(all_candidates.stdout)} == {
        "approved",
        "rejected",
    }

    inverse_reject = invoke(
        "candidate",
        "reject",
        str(approved.id),
        "--reviewer",
        "mallory",
        "--reason",
        "undo",
        "-d",
        str(database),
    )
    inverse_approve = invoke(
        "candidate",
        "approve",
        str(rejected.id),
        "--reviewer",
        "mallory",
        "-d",
        str(database),
    )
    assert inverse_reject.exit_code == inverse_approve.exit_code == 2
    assert "decisions are terminal" in inverse_reject.stderr
    assert "decisions are terminal" in inverse_approve.stderr

    for candidate, expected in ((approved, approval_review), (rejected, rejection_review)):
        shown = invoke("candidate", "show", str(candidate.id), "--json", "-d", str(database))
        history = invoke("candidate", "history", str(candidate.id), "--json", "-d", str(database))
        assert shown.exit_code == history.exit_code == 0
        assert (
            ConversationCandidate.model_validate_json(shown.stdout).status.value
            == expected.decision.value
        )
        assert [ConversationReview.model_validate(item) for item in json.loads(history.stdout)] == [
            expected
        ]


def test_candidate_filters_default_pending_status_all_conversation_and_version(
    tmp_path: Path,
) -> None:
    _, database, candidates = import_candidates(tmp_path)
    approved = candidates[0]
    assert (
        invoke(
            "candidate",
            "approve",
            str(approved.id),
            "--reviewer",
            "reviewer",
            "-d",
            str(database),
        ).exit_code
        == 0
    )

    pending = json.loads(invoke("candidate", "list", "--json", "-d", str(database)).stdout)
    approved_only = json.loads(
        invoke("candidate", "list", "--status", "approved", "--json", "-d", str(database)).stdout
    )
    all_filtered = json.loads(
        invoke(
            "candidate",
            "list",
            "--status",
            "all",
            "--conversation-id",
            "candidate-cli",
            "--version",
            "1",
            "--json",
            "-d",
            str(database),
        ).stdout
    )
    wrong_conversation = json.loads(
        invoke(
            "candidate",
            "list",
            "--status",
            "all",
            "--conversation-id",
            "other",
            "--json",
            "-d",
            str(database),
        ).stdout
    )

    assert [item["status"] for item in pending] == ["pending"]
    assert [item["id"] for item in approved_only] == [str(approved.id)]
    assert len(all_filtered) == 2 and wrong_conversation == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("candidate", "list", "--status", "unknown"),
        ("candidate", "list", "--conversation-id", "   "),
        ("candidate", "list", "--version", "0"),
        ("candidate", "show", "not-a-uuid"),
        ("candidate", "history", "not-a-uuid"),
        (
            "candidate",
            "approve",
            "00000000-0000-0000-0000-000000000000",
            "--reviewer",
            "   ",
        ),
        (
            "candidate",
            "approve",
            "00000000-0000-0000-0000-000000000000",
            "--reviewer",
            "reviewer",
            "--reason",
            "   ",
        ),
        (
            "candidate",
            "reject",
            "00000000-0000-0000-0000-000000000000",
            "--reviewer",
            "reviewer",
            "--reason",
            "   ",
        ),
    ],
)
def test_invalid_candidate_arguments_exit_two_before_database_creation(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    database = tmp_path / "unopened" / "knowledge.db"
    result = invoke(*arguments, "-d", str(database))
    assert result.exit_code == 2
    assert not database.exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", ["show", "history"])
def test_missing_candidate_uses_stable_exit_two(tmp_path: Path, command: str) -> None:
    candidate_id = uuid4()
    result = invoke("candidate", command, str(candidate_id), "-d", str(tmp_path / "knowledge.db"))
    assert result.exit_code == 2
    assert f"candidate {candidate_id} was not found" in result.stderr


def test_candidate_review_is_non_interactive_and_requires_options(tmp_path: Path) -> None:
    _, database, candidates = import_candidates(tmp_path)
    missing_reviewer = invoke("candidate", "approve", str(candidates[0].id), "-d", str(database))
    missing_reason = invoke(
        "candidate",
        "reject",
        str(candidates[1].id),
        "--reviewer",
        "reviewer",
        "-d",
        str(database),
    )
    assert missing_reviewer.exit_code == missing_reason.exit_code == 2
    assert "Missing option" in missing_reviewer.output
    assert "Missing option" in missing_reason.output
