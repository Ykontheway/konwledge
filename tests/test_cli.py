from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from konwledge import __version__
from konwledge.cli import app

runner = CliRunner()


def invoke(*arguments: str) -> Result:
    return runner.invoke(app, list(arguments))


def test_package_has_pre_alpha_version() -> None:
    assert __version__ == "0.0.0"


def test_cli_without_arguments_prints_help() -> None:
    result = invoke()

    assert result.exit_code == 0
    assert "Import and inspect traceable local knowledge" in result.stdout
    assert "import" in result.stdout
    assert "list" in result.stdout
    assert "show" in result.stdout


def test_cli_reports_version() -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert result.stdout.strip() == "knowledge 0.0.0"


def test_empty_list_initializes_database_and_is_friendly(tmp_path: Path) -> None:
    database = tmp_path / "state" / "knowledge.db"

    result = invoke("list", "--database", str(database))

    assert result.exit_code == 0
    assert "Knowledge library is empty" in result.stdout
    assert database.is_file()


def test_directory_import_is_recursive_stable_and_skips_unsupported(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    nested = corpus / "nested"
    nested.mkdir(parents=True)
    (corpus / "b.txt").write_text("Second.", encoding="utf-8")
    (corpus / "a.md").write_text("# First\nBody.\n", encoding="utf-8")
    (nested / "c.markdown").write_text("# Third\nBody.\n", encoding="utf-8")
    (nested / "ignored.json").write_text("{}", encoding="utf-8")
    database = tmp_path / "knowledge.db"

    result = invoke("import", str(corpus), "--database", str(database))

    assert result.exit_code == 0
    assert (
        result.stdout.index("a.md")
        < result.stdout.index("b.txt")
        < result.stdout.index("c.markdown")
    )
    assert "3 file(s)" in result.stdout
    assert "1 unsupported file(s) skipped" in result.stdout
    assert "ignored.json" in result.stdout


def test_single_file_import_is_idempotent_and_list_show_work(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Heading\nOriginal passage.\n", encoding="utf-8")
    database = tmp_path / "knowledge.db"

    first = invoke("import", str(source), "-d", str(database))
    second = invoke("import", str(source), "-d", str(database))
    listed = invoke("list", "-d", str(database))

    assert first.exit_code == second.exit_code == listed.exit_code == 0
    assert "1 document(s) and 1 unit(s) created" in first.stdout
    assert "unchanged:" in second.stdout
    assert "0 document(s) and 0 unit(s) created" in second.stdout
    assert listed.stdout.count("document ") == 1
    assert "units: 1" in listed.stdout
    assert "source: file" in listed.stdout
    assert "locator:" in listed.stdout
    assert "fingerprint:" in listed.stdout

    unit_line = next(
        line for line in listed.stdout.splitlines() if line.strip().startswith("unit ")
    )
    unit_id = unit_line.split()[1]
    shown = invoke("show", unit_id, "-d", str(database))

    assert shown.exit_code == 0
    assert f"unit id: {unit_id}" in shown.stdout
    assert "section_path: Heading" in shown.stdout
    assert "heading: Heading" in shown.stdout
    assert "range: [0, 28)" in shown.stdout
    assert "# Heading\nOriginal passage.\n" in shown.stdout


def test_changed_content_creates_new_document_and_units(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    database = tmp_path / "knowledge.db"
    source.write_text("first", encoding="utf-8")
    assert invoke("import", str(source), "-d", str(database)).exit_code == 0

    source.write_text("second", encoding="utf-8")
    changed = invoke("import", str(source), "-d", str(database))
    listed = invoke("list", "-d", str(database))

    assert changed.exit_code == 0
    assert "1 document(s) and 1 unit(s) created" in changed.stdout
    assert listed.stdout.count("document ") == 2
    assert listed.stdout.count("units: 1") == 2


@pytest.mark.parametrize("unit_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_show_rejects_invalid_or_missing_id(tmp_path: Path, unit_id: str) -> None:
    result = invoke("show", unit_id, "-d", str(tmp_path / "knowledge.db"))

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    if unit_id.startswith("0000"):
        assert "was not found" in combined
    else:
        assert "Invalid value" in combined


def test_import_rejects_unsupported_file_and_empty_directory(tmp_path: Path) -> None:
    unsupported = tmp_path / "notes.pdf"
    unsupported.write_bytes(b"pdf")
    empty = tmp_path / "empty"
    empty.mkdir()
    database = tmp_path / "knowledge.db"

    unsupported_result = invoke("import", str(unsupported), "-d", str(database))
    empty_result = invoke("import", str(empty), "-d", str(database))

    assert unsupported_result.exit_code == 2
    assert "unsupported file extension" in (unsupported_result.stdout + unsupported_result.stderr)
    assert empty_result.exit_code == 2
    assert "no supported files found" in (empty_result.stdout + empty_result.stderr)
