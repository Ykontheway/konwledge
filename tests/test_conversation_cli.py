"""Conversation import CLI acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from konwledge.cli import app
from konwledge.conversations import (
    ConversationImportResult,
    StoredConversation,
    load_conversation_json,
)

runner = CliRunner()


def invoke(*arguments: str, input_text: str | None = None) -> Result:
    return runner.invoke(app, list(arguments), input=input_text)


def payload(*, version: int = 1, suffix: str = "") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "conversation_id": "cli-conversation",
        "version": version,
        "created_at": "2026-11-11T10:00:00Z",
        "messages": [
            {
                "id": f"user-{version}",
                "role": "user",
                "content": f"事实：用户偏好深色模式{suffix}\n问题：这会自动成为知识吗？",
            },
            {
                "id": f"assistant-{version}",
                "role": "assistant",
                "content": f"决策：默认使用 SQLite{suffix}",
            },
            {"id": f"system-{version}", "role": "system", "content": "事实：忽略系统消息"},
            {"id": f"tool-{version}", "role": "tool", "content": "决策：忽略工具消息"},
        ],
        "metadata": {"synthetic": True},
    }


def write_payload(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [
        ('{"schema_version":1,"schema_version":1}', "duplicate object key"),
        ('{"schema_version":1,"messages":[}', "not valid JSON"),
        ('{"schema_version":1,"conversation_id":"private-value"}', "does not match schema"),
        ('["not-an-object"]', "root must be an object"),
    ],
)
def test_invalid_import_is_rejected_before_database_creation(
    tmp_path: Path, content: str, diagnostic: str
) -> None:
    source = tmp_path / "invalid.json"
    source.write_text(content, encoding="utf-8")
    database = tmp_path / "must-not-exist" / "knowledge.db"

    result = invoke("conversation", "import", str(source), "--json", "-d", str(database))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert diagnostic in result.stderr
    assert "private-value" not in result.stderr
    assert not database.exists() and not database.parent.exists()


def test_oversize_and_non_utf8_imports_do_not_open_storage(tmp_path: Path) -> None:
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (10 * 1024 * 1024 + 1))
    non_utf8 = tmp_path / "non-utf8.json"
    non_utf8.write_bytes(b"\xff")

    for source, code, diagnostic in (
        (oversize, 2, "exceeds 10485760 bytes"),
        (non_utf8, 2, "valid UTF-8"),
    ):
        database = tmp_path / source.stem / "knowledge.db"
        result = invoke("conversation", "import", str(source), "-d", str(database))
        assert result.exit_code == code
        assert diagnostic in result.stderr
        assert not database.exists() and not database.parent.exists()


def test_file_and_stdin_import_json_are_valid_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "conversation 中文.json"
    value = payload()
    write_payload(source, value)
    database = tmp_path / "knowledge.db"

    imported = invoke("conversation", "import", str(source), "--json", "-d", str(database))
    repeated = invoke(
        "conversation",
        "import",
        "-",
        "--json",
        "-d",
        str(database),
        input_text=json.dumps(value, ensure_ascii=False),
    )

    assert imported.exit_code == repeated.exit_code == 0
    first = ConversationImportResult.model_validate_json(imported.stdout)
    second = ConversationImportResult.model_validate_json(repeated.stdout)
    assert first.created is True and second.created is False
    assert first.stored == second.stored
    assert len(first.candidates) == 2


def test_list_show_history_and_export_default_to_latest_version(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    first_path = tmp_path / "v1.json"
    second_path = tmp_path / "v2.json"
    write_payload(first_path, payload())
    write_payload(second_path, payload(version=2, suffix=" v2"))
    assert invoke("conversation", "import", str(first_path), "-d", str(database)).exit_code == 0
    assert invoke("conversation", "import", str(second_path), "-d", str(database)).exit_code == 0

    listed = invoke("conversation", "list", "--json", "-d", str(database))
    shown = invoke("conversation", "show", "cli-conversation", "--json", "-d", str(database))
    history = invoke("conversation", "history", "cli-conversation", "--json", "-d", str(database))
    exported = invoke("conversation", "export", "cli-conversation", "-d", str(database))

    assert listed.exit_code == shown.exit_code == history.exit_code == exported.exit_code == 0
    assert [item["conversation"]["version"] for item in json.loads(listed.stdout)] == [1, 2]
    assert StoredConversation.model_validate_json(shown.stdout).conversation.version == 2
    assert [item["conversation"]["version"] for item in json.loads(history.stdout)] == [1, 2]
    roundtrip = load_conversation_json(exported.stdout)
    assert roundtrip.version == 2 and roundtrip == load_conversation_json(
        json.dumps(payload(version=2, suffix=" v2"), ensure_ascii=False)
    )

    explicit = invoke(
        "conversation",
        "export",
        "cli-conversation",
        "--version",
        "1",
        "-d",
        str(database),
    )
    assert load_conversation_json(explicit.stdout).version == 1


def test_export_uses_safe_output_guard(tmp_path: Path) -> None:
    source = tmp_path / "conversation.json"
    write_payload(source, payload())
    database = tmp_path / "knowledge.db"
    assert invoke("conversation", "import", str(source), "-d", str(database)).exit_code == 0
    output = tmp_path / "export.json"
    output.write_text("keep", encoding="utf-8")

    refused = invoke(
        "conversation",
        "export",
        "cli-conversation",
        "--output",
        str(output),
        "-d",
        str(database),
    )
    replaced = invoke(
        "conversation",
        "export",
        "cli-conversation",
        "--output",
        str(output),
        "--overwrite",
        "-d",
        str(database),
    )

    assert refused.exit_code == 2 and "--overwrite" in refused.stderr
    assert replaced.exit_code == 0
    assert load_conversation_json(output.read_text(encoding="utf-8")).version == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ("conversation", "show", "   "),
        ("conversation", "history", "   "),
        ("conversation", "export", "   "),
        ("conversation", "show", "missing", "--version", "0"),
    ],
)
def test_conversation_argument_errors_exit_two_before_database_creation(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    database = tmp_path / "unopened" / "knowledge.db"
    result = invoke(*arguments, "-d", str(database))
    assert result.exit_code == 2
    assert not database.exists()


def test_missing_conversation_is_a_stable_validation_failure(tmp_path: Path) -> None:
    result = invoke(
        "conversation",
        "show",
        "missing",
        "-d",
        str(tmp_path / "knowledge.db"),
    )
    assert result.exit_code == 2
    assert "was not found" in result.stderr
    assert "Traceback" not in result.output
