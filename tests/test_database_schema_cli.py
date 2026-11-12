"""U20 database-schema CLI acceptance tests."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from konwledge.cli import app
from konwledge.database_schema import DatabaseConnectionError, DatabaseSchemaSnapshot

runner = CliRunner()


def invoke(*arguments: str, env: dict[str, str] | None = None) -> Result:
    return runner.invoke(app, list(arguments), env=env)


def create_target(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, old_field TEXT);
            CREATE TABLE sentinel(payload TEXT);
            INSERT INTO sentinel(payload) VALUES ('TOP_SECRET_BUSINESS_ROW');
            """
        )
        connection.commit()
    finally:
        connection.close()


def capture(target: Path, database: Path, *, title: str = "Synthetic schema") -> dict[str, Any]:
    result = invoke(
        "database",
        "capture-sqlite",
        str(target),
        "--name",
        title,
        "--json",
        "-d",
        str(database),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def table_counts(database: Path) -> tuple[int, ...]:
    connection = sqlite3.connect(database)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "documents",
                "knowledge_units",
                "revisions",
                "database_schema_snapshots",
            )
        )
    finally:
        connection.close()


def test_sqlite_capture_history_diff_search_and_export_are_incremental(tmp_path: Path) -> None:
    target = tmp_path / "source.sqlite"
    database = tmp_path / "metadata.sqlite"
    create_target(target)

    first = capture(target, database)
    repeated = capture(target, database)
    assert first["version"] == repeated["version"] == 1
    assert first["created"] is True
    assert repeated["created"] is False and repeated["schema_same"] is True
    source_id = first["source_id"]

    connection = sqlite3.connect(target)
    try:
        connection.execute("ALTER TABLE accounts ADD COLUMN active_field TEXT")
        connection.execute("CREATE TABLE temporary_table(id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    second = capture(target, database)
    assert second["version"] == 2

    connection = sqlite3.connect(target)
    try:
        connection.execute("DROP TABLE temporary_table")
        connection.execute("ALTER TABLE accounts DROP COLUMN old_field")
        connection.commit()
    finally:
        connection.close()
    third = capture(target, database)
    assert third["version"] == 3

    listed = invoke("database", "list", "--json", "-d", str(database))
    history = invoke("database", "history", source_id, "--json", "-d", str(database))
    shown = invoke("database", "show", source_id, "--version", "3", "--json", "-d", str(database))
    diff_added = invoke(
        "database",
        "diff",
        source_id,
        "--from-version",
        "1",
        "--to-version",
        "2",
        "--json",
        "-d",
        str(database),
    )
    diff_removed = invoke(
        "database",
        "diff",
        source_id,
        "--from-version",
        "2",
        "--to-version",
        "3",
        "--json",
        "-d",
        str(database),
    )
    for result in (listed, history, shown, diff_added, diff_removed):
        assert result.exit_code == 0, result.output

    assert json.loads(listed.stdout)[0]["latest_version"] == 3
    assert [item["version"] for item in json.loads(history.stdout)] == [1, 2, 3]
    shown_record = json.loads(shown.stdout)
    DatabaseSchemaSnapshot.model_validate(shown_record["snapshot"])
    assert shown_record["source_id"] == source_id
    added_payload = json.loads(diff_added.stdout)
    removed_payload = json.loads(diff_removed.stdout)
    assert any("temporary_table" in key for key in added_payload["added_tables"])
    assert added_payload["changed_tables"][0]["added_columns"] == ["active_field"]
    assert any("temporary_table" in key for key in removed_payload["removed_tables"])
    assert removed_payload["changed_tables"][0]["removed_columns"] == ["old_field"]

    exported = invoke("database", "export", source_id, "--version", "3", "-d", str(database))
    assert exported.exit_code == 0
    assert "active_field" in exported.stdout
    assert "old_field" not in exported.stdout
    assert "TOP_SECRET_BUSINESS_ROW" not in exported.stdout

    search = invoke("search", "old_field", "--json", "-d", str(database))
    context = invoke("context", "old_field", "--json", "-d", str(database))
    assert search.exit_code == context.exit_code == 0
    assert json.loads(search.stdout)["hits"] == []
    assert json.loads(context.stdout)["blocks"] == []


def test_invalid_sqlite_input_never_creates_metadata_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    cases = (
        (str(missing), "Valid", "5"),
        (str(tmp_path), "Valid", "5"),
        (str(missing), "   ", "5"),
        (str(missing), "Valid", "0"),
        (str(missing), "Valid", "nan"),
        (str(missing), "Valid", "61"),
    )
    for index, (target, title, timeout) in enumerate(cases):
        database = tmp_path / f"case-{index}" / "metadata.sqlite"
        result = invoke(
            "database",
            "capture-sqlite",
            target,
            "--name",
            title,
            "--timeout",
            timeout,
            "-d",
            str(database),
        )
        assert result.exit_code == 2
        assert not database.exists() and not database.parent.exists()


def test_sqlite_rejects_metadata_file_symlink_and_hardlink(tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.sqlite"
    assert invoke("database", "list", "-d", str(metadata)).exit_code == 0
    aliases = [tmp_path / "metadata-symlink.sqlite", tmp_path / "metadata-hardlink.sqlite"]
    aliases[0].symlink_to(metadata)
    aliases[1].hardlink_to(metadata)
    size = metadata.stat().st_size

    for alias in aliases:
        result = invoke(
            "database", "capture-sqlite", str(alias), "--name", "Forbidden", "-d", str(metadata)
        )
        assert result.exit_code == 2
        assert "Knowledge Library" in result.stderr
        assert metadata.stat().st_size == size


def test_postgres_validates_before_storage_and_never_accepts_a_dsn_option(tmp_path: Path) -> None:
    database = tmp_path / "unopened" / "metadata.sqlite"
    help_result = invoke("database", "capture-postgres", "--help")
    assert help_result.exit_code == 0
    assert "--dsn-env" in help_result.stdout
    assert "--dsn " not in help_result.stdout

    cases = (
        ("BAD-NAME", "Valid", "public"),
        ("GOOD_NAME", "   ", "public"),
        ("GOOD_NAME", "Valid", ""),
        ("GOOD_NAME", "Valid", "pg_catalog"),
    )
    for env_name, title, schema in cases:
        result = invoke(
            "database",
            "capture-postgres",
            "--dsn-env",
            env_name,
            "--name",
            title,
            "--schema",
            schema,
            "-d",
            str(database),
        )
        assert result.exit_code == 2
        assert not database.exists() and not database.parent.exists()


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg"])
def test_postgres_missing_extra_is_friendly_and_credentials_are_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    secret = "credential-must-not-appear"
    dsn = f"{scheme}://private-user:{secret}@localhost/example"

    def missing_driver(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ModuleNotFoundError("psycopg")

    monkeypatch.setattr("konwledge.database_schema.collector.create_engine", missing_driver)
    database = tmp_path / "metadata.sqlite"
    result = invoke(
        "database",
        "capture-postgres",
        "--dsn-env",
        "SYNTHETIC_POSTGRES_DSN",
        "--name",
        "PostgreSQL schema",
        "-d",
        str(database),
        env={"SYNTHETIC_POSTGRES_DSN": dsn},
    )
    assert result.exit_code == 1
    assert "optional 'postgres' extra" in result.stderr
    assert secret not in result.output and "private-user" not in result.output
    assert not database.exists()


def test_postgres_runtime_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "secret-runtime-value"

    def fail_capture(self: object) -> object:
        del self
        raise DatabaseConnectionError("PostgreSQL schema capture failed")

    monkeypatch.setattr(
        "konwledge.newcli_database_schema.PostgreSQLSchemaCollector.capture", fail_capture
    )
    result = invoke(
        "database",
        "capture-postgres",
        "--dsn-env",
        "PG_CAPTURE_DSN",
        "--name",
        "Runtime failure",
        "-d",
        str(tmp_path / "metadata.sqlite"),
        env={"PG_CAPTURE_DSN": f"postgresql+psycopg://alice:{secret}@localhost/app"},
    )
    assert result.exit_code == 1
    assert result.stderr.strip() == "Error: PostgreSQL schema capture failed"
    assert secret not in result.output and "alice" not in result.output


def test_export_requires_explicit_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "source.sqlite"
    database = tmp_path / "metadata.sqlite"
    create_target(target)
    source_id = capture(target, database)["source_id"]
    output = tmp_path / "schema.json"
    output.write_text("keep", encoding="utf-8")

    refused = invoke(
        "database",
        "export",
        source_id,
        "--version",
        "1",
        "--output",
        str(output),
        "-d",
        str(database),
    )
    assert refused.exit_code == 2
    assert output.read_text(encoding="utf-8") == "keep"
    replaced = invoke(
        "database",
        "export",
        source_id,
        "--version",
        "1",
        "--output",
        str(output),
        "--overwrite",
        "-d",
        str(database),
    )
    assert replaced.exit_code == 0
    DatabaseSchemaSnapshot.model_validate_json(output.read_text(encoding="utf-8"))


def test_list_and_history_do_not_change_metadata_rows(tmp_path: Path) -> None:
    target = tmp_path / "source.sqlite"
    database = tmp_path / "metadata.sqlite"
    create_target(target)
    source_id = capture(target, database)["source_id"]
    before = table_counts(database)

    listed = invoke("database", "list", "--json", "-d", str(database))
    history = invoke("database", "history", source_id, "--json", "-d", str(database))

    assert listed.exit_code == history.exit_code == 0
    assert table_counts(database) == before


def test_missing_snapshot_and_invalid_diff_are_exit_two(tmp_path: Path) -> None:
    database = tmp_path / "metadata.sqlite"
    missing_id = "00000000-0000-0000-0000-000000000000"
    missing = invoke("database", "show", missing_id, "--version", "1", "-d", str(database))
    same = invoke(
        "database",
        "diff",
        missing_id,
        "--from-version",
        "1",
        "--to-version",
        "1",
        "-d",
        str(database),
    )
    assert missing.exit_code == same.exit_code == 2
    assert "Traceback" not in missing.output + same.output


def test_no_environment_secret_is_left_in_process_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "unprinted-secret"
    monkeypatch.setenv("PRIVATE_CAPTURE_DSN", secret)
    result = invoke(
        "database",
        "capture-postgres",
        "--dsn-env",
        "PRIVATE_CAPTURE_DSN",
        "--name",
        "Invalid DSN",
        "-d",
        str(tmp_path / "metadata.sqlite"),
    )
    assert result.exit_code == 2
    assert secret not in result.output
    assert not (tmp_path / "metadata.sqlite").exists()
    assert os.environ["PRIVATE_CAPTURE_DSN"] == secret
