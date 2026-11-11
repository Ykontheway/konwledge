from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from konwledge import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
LOCK_PATH = PROJECT_ROOT / "uv.lock"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _run_python(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_metadata_is_coherent() -> None:
    pyproject = _load_toml(PYPROJECT_PATH)
    project = pyproject["project"]
    assert isinstance(project, dict)

    assert project["name"] == "konwledge"
    assert project["version"] == __version__ == "0.1.0"
    assert project["requires-python"] == ">=3.11"
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    assert project["scripts"] == {
        "knowledge": "konwledge.cli:main",
        "konwledge": "konwledge.cli:main",
    }


def test_lock_contains_matching_editable_project_record() -> None:
    pyproject = _load_toml(PYPROJECT_PATH)
    lock = _load_toml(LOCK_PATH)
    project = pyproject["project"]
    assert isinstance(project, dict)

    editable_projects = [
        package
        for package in lock["package"]
        if package["name"] == project["name"] and package.get("source") == {"editable": "."}
    ]
    assert len(editable_projects) == 1
    assert editable_projects[0]["version"] == project["version"]
    assert lock["requires-python"] == project["requires-python"]


def test_packaged_demo_corpus_is_available_via_importlib_resources() -> None:
    corpus = resources.files("konwledge.demo").joinpath("corpus")
    expected = {
        "deployment-guide.v1.md",
        "deployment-guide.v2.md",
        "deployment-guide.v3.md",
        "oncall-faq.v1.md",
    }

    assert {item.name for item in corpus.iterdir() if item.is_file()} == expected
    assert all(corpus.joinpath(name).read_text(encoding="utf-8").strip() for name in expected)


def test_root_public_api_is_lazy_and_matches_application_facade(tmp_path: Path) -> None:
    script = """
import sys
import konwledge

assert konwledge.__all__ == ["KnowledgeLibrary", "__version__"]
assert {"KnowledgeLibrary", "__version__"} <= set(dir(konwledge))
assert "konwledge.application" not in sys.modules
implementation = konwledge.KnowledgeLibrary
from konwledge.application import KnowledgeLibrary
assert implementation is KnowledgeLibrary
assert konwledge.KnowledgeLibrary is KnowledgeLibrary
try:
    konwledge.not_a_public_attribute
except AttributeError:
    pass
else:
    raise AssertionError("unknown root attributes must raise AttributeError")
"""

    result = _run_python(script, tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_version_command_needs_no_optional_lancedb_or_database(tmp_path: Path) -> None:
    script = """
import sys
sys.modules["lancedb"] = None
from konwledge.cli import main
raise SystemExit(main(["--version"]))
"""

    result = _run_python(script, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "knowledge 0.1.0"
    assert list(tmp_path.iterdir()) == []


def test_packaged_type_marker_is_present() -> None:
    assert resources.files("konwledge").joinpath("py.typed").is_file()
