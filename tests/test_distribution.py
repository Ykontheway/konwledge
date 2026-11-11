from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.installed_smoke import is_installed_location
from scripts.verify_distribution import (
    Inspection,
    inspect_sdist,
    inspect_wheel,
    load_project_metadata,
)

NAME = "konwledge"
VERSION = "0.1.0"
RESOURCE = "konwledge/demo/corpus/demo.md"
WHEEL_METADATA = "Metadata-Version: 2.4\nName: konwledge\nVersion: 0.1.0\n"
SDIST_METADATA = "Metadata-Version: 2.4\nName: konwledge\nVersion: 0.1.0\n"
ENTRY_POINTS = "[console_scripts]\nknowledge = konwledge.cli:main\nkonwledge = konwledge.cli:main\n"


def write_wheel(
    path: Path, *, overrides: dict[str, str] | None = None, omit: tuple[str, ...] = ()
) -> None:
    files = {
        "konwledge/__init__.py": "",
        "konwledge/__main__.py": "",
        "konwledge/py.typed": "",
        RESOURCE: "demo",
        "konwledge-0.1.0.dist-info/METADATA": WHEEL_METADATA,
        "konwledge-0.1.0.dist-info/entry_points.txt": ENTRY_POINTS,
    }
    files.update(overrides or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            if name in omit:
                continue
            archive.writestr(name, content)


def write_sdist(
    path: Path, *, overrides: dict[str, str] | None = None, omit: tuple[str, ...] = ()
) -> None:
    root = "konwledge-0.1.0"
    files = {
        f"{root}/PKG-INFO": SDIST_METADATA,
        f"{root}/README.md": "readme",
        f"{root}/LICENSE": "license",
        f"{root}/pyproject.toml": "[project]",
        f"{root}/src/konwledge/__init__.py": "",
        f"{root}/src/konwledge/py.typed": "",
        f"{root}/tests/test_source.py": "",
    }
    files.update(overrides or {})
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            if name in omit:
                continue
            payload = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_project_identity_comes_from_pyproject() -> None:
    assert load_project_metadata() == (NAME, VERSION)


def test_installed_location_rejects_source_checkout(tmp_path: Path) -> None:
    prefix = tmp_path / "venv"
    installed = prefix / "lib" / "python3.11" / "site-packages" / "konwledge" / "__init__.py"
    source = tmp_path / "checkout" / "src" / "konwledge" / "__init__.py"
    installed.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    installed.write_text("")
    source.write_text("")

    assert is_installed_location(installed, prefix, (installed.parents[1],))
    assert not is_installed_location(source, prefix, (installed.parents[1],))


def test_inspect_wheel_accepts_metadata_resources_and_both_entry_points(tmp_path: Path) -> None:
    wheel = tmp_path / "konwledge-0.1.0-py3-none-any.whl"
    write_wheel(wheel)

    result = inspect_wheel(wheel, NAME, VERSION, (RESOURCE,))

    assert result == Inspection(
        format="wheel",
        checks=(
            "safe_paths",
            "metadata_identity",
            "console_entry_points",
            "package_modules",
            "demo_resources",
            "runtime_exclusions",
        ),
        file_count=6,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"konwledge-0.1.0.dist-info/METADATA": WHEEL_METADATA.replace(VERSION, "9.9.9")},
            "version mismatch",
        ),
        ({"tests/test_leak.py": ""}, "forbidden member"),
        ({"konwledge/state.db": ""}, "forbidden member"),
        ({"konwledge/private.pem": "secret"}, "forbidden member"),
        ({"../escape": ""}, "unsafe archive member"),
    ],
)
def test_inspect_wheel_rejects_bad_metadata_or_leaked_files(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    wheel = tmp_path / "bad.whl"
    write_wheel(wheel, overrides=overrides)

    with pytest.raises(ValueError, match=message):
        inspect_wheel(wheel, NAME, VERSION, (RESOURCE,))


def test_inspect_wheel_rejects_missing_resource_and_entry_point(tmp_path: Path) -> None:
    wheel = tmp_path / "bad.whl"
    write_wheel(
        wheel,
        overrides={
            "konwledge-0.1.0.dist-info/entry_points.txt": (
                "[console_scripts]\nknowledge = konwledge.cli:main\n"
            )
        },
    )
    with pytest.raises(ValueError, match="entry points mismatch"):
        inspect_wheel(wheel, NAME, VERSION, (RESOURCE,))

    missing = tmp_path / "missing.whl"
    write_wheel(missing)
    with pytest.raises(ValueError, match="missing packaged file"):
        inspect_wheel(missing, NAME, VERSION, (RESOURCE, "konwledge/demo/corpus/missing.md"))


def test_inspect_sdist_allows_source_tests_with_narrow_warning(tmp_path: Path) -> None:
    sdist = tmp_path / "konwledge-0.1.0.tar.gz"
    write_sdist(sdist)

    result = inspect_sdist(sdist, NAME, VERSION)

    assert result.format == "sdist"
    assert result.file_count == 7
    assert len(result.warnings) == 1
    assert "source tests" in result.warnings[0]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"konwledge-0.1.0/PKG-INFO": SDIST_METADATA.replace(NAME, "other")}, "name mismatch"),
        ({"konwledge-0.1.0/.venv/bin/python": ""}, "forbidden member"),
        ({"konwledge-0.1.0/cache.sqlite": ""}, "forbidden member"),
        ({"konwledge-0.1.0/id_rsa": "secret"}, "forbidden member"),
        ({"../../escape": ""}, "unsafe archive member"),
    ],
)
def test_inspect_sdist_rejects_bad_metadata_runtime_files_and_unsafe_paths(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    sdist = tmp_path / "bad.tar.gz"
    write_sdist(sdist, overrides=overrides)

    with pytest.raises(ValueError, match=message):
        inspect_sdist(sdist, NAME, VERSION)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_distributions_require_type_marker(tmp_path: Path, kind: str) -> None:
    if kind == "wheel":
        wheel = tmp_path / "missing-marker.whl"
        write_wheel(wheel, omit=("konwledge/py.typed",))
        with pytest.raises(ValueError, match=r"py\.typed"):
            inspect_wheel(wheel, NAME, VERSION, (RESOURCE,))
    else:
        sdist = tmp_path / "missing-marker.tar.gz"
        write_sdist(sdist, omit=("konwledge-0.1.0/src/konwledge/py.typed",))
        with pytest.raises(ValueError, match=r"py\.typed"):
            inspect_sdist(sdist, NAME, VERSION)
