from __future__ import annotations

from pathlib import Path

import pytest

from konwledge.application import ImportPathError, discover_import_files


def test_discover_import_files_sorts_relative_paths_and_extensions_case_insensitively(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "Z"
    nested.mkdir()
    (tmp_path / "b.TXT").write_text("b", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (nested / "c.MARKDOWN").write_text("c", encoding="utf-8")
    (tmp_path / "ignore.csv").write_text("ignored", encoding="utf-8")

    discovery = discover_import_files(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in discovery.files] == [
        "Z/c.MARKDOWN",
        "a.md",
        "b.TXT",
    ]
    assert [path.name for path in discovery.skipped] == ["ignore.csv"]


def test_discover_import_files_accepts_one_supported_file(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")

    assert discover_import_files(source).files == (source.resolve(),)


@pytest.mark.parametrize("name", ["missing", "unsupported.pdf"])
def test_discover_import_files_reports_unusable_target(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    if target.suffix:
        target.write_text("content", encoding="utf-8")

    with pytest.raises(ImportPathError):
        discover_import_files(target)
