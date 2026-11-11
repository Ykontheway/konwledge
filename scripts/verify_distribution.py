"""Build and verify wheel/sdist artifacts in genuinely isolated installations."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "installed_smoke.py"
API_SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "installed_api_smoke.py"
API_MODULES = tuple(f"konwledge/api/{name}.py" for name in ("__init__", "app", "models", "server"))
FIXTURE_NAMES = (
    "evaluation-demo.json",
    "evaluation-baseline.json",
    "evaluation-gate.json",
)
CONSOLE_SCRIPTS = {
    "knowledge": "konwledge.cli:main",
    "konwledge": "konwledge.cli:main",
}
RUNTIME_COMPONENTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
SECRET_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


@dataclass(frozen=True)
class Inspection:
    """A safe artifact inspection result."""

    format: str
    checks: tuple[str, ...]
    file_count: int
    warnings: tuple[str, ...] = ()


def canonicalize_name(value: str) -> str:
    """Apply the package-name normalization used by distribution metadata."""

    return re.sub(r"[-_.]+", "-", value).lower()


def load_project_metadata(path: Path = PYPROJECT) -> tuple[str, str]:
    """Read the expected distribution identity from pyproject.toml."""

    with path.open("rb") as stream:
        project = tomllib.load(stream)["project"]
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise ValueError("pyproject project.name and project.version must be strings")
    return name, version


def _safe_member_name(raw_name: str) -> str:
    name = raw_name.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe archive member path: {raw_name!r}")
    if ":" in path.parts[0]:
        raise ValueError(f"unsafe archive member path: {raw_name!r}")
    return path.as_posix()


def _forbidden_members(names: Sequence[str], *, wheel: bool) -> list[str]:
    findings: list[str] = []
    for name in names:
        safe = _safe_member_name(name)
        parts = tuple(part.casefold() for part in PurePosixPath(safe).parts)
        basename = parts[-1]
        if any(part in RUNTIME_COMPONENTS for part in parts):
            findings.append(safe)
            continue
        if wheel and "tests" in parts:
            findings.append(safe)
            continue
        if basename.endswith((".db", ".sqlite", ".sqlite3")):
            findings.append(safe)
            continue
        if basename in SECRET_NAMES or PurePosixPath(basename).suffix in SECRET_SUFFIXES:
            findings.append(safe)
    return findings


def _metadata_identity(text: str, expected_name: str, expected_version: str) -> None:
    metadata = Parser().parsestr(text)
    actual_name = metadata.get("Name")
    actual_version = metadata.get("Version")
    if canonicalize_name(actual_name or "") != canonicalize_name(expected_name):
        raise ValueError(f"distribution name mismatch: {actual_name!r}")
    if actual_version != expected_version:
        raise ValueError(f"distribution version mismatch: {actual_version!r}")
    if "api" not in metadata.get_all("Provides-Extra", []):
        raise ValueError("distribution is missing the api extra")
    requirements = metadata.get_all("Requires-Dist", [])
    api_dependencies: set[str] = set()
    for requirement in requirements:
        dependency = canonicalize_name(re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0])
        if dependency in {"openai", "langchain-openai", "httpx"}:
            raise ValueError("distribution contains SDK/test runtime dependencies")
        if dependency in {"fastapi", "uvicorn"}:
            if not re.search(r"extra\s*==\s*['\"]api['\"]", requirement):
                raise ValueError("API dependencies must be conditional on the api extra")
            api_dependencies.add(dependency)
    if api_dependencies != {"fastapi", "uvicorn"}:
        raise ValueError("api extra must include FastAPI and Uvicorn")


def inspect_wheel(
    path: Path,
    expected_name: str,
    expected_version: str,
    expected_resources: Sequence[str],
) -> Inspection:
    """Inspect a wheel without importing or extracting it."""

    with zipfile.ZipFile(path) as archive:
        names = tuple(
            _safe_member_name(name) for name in archive.namelist() if not name.endswith("/")
        )
        forbidden = _forbidden_members(names, wheel=True)
        if forbidden:
            raise ValueError(f"wheel contains forbidden member: {forbidden[0]}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one dist-info/METADATA")
        _metadata_identity(
            archive.read(metadata_names[0]).decode("utf-8"), expected_name, expected_version
        )
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_names) != 1:
            raise ValueError("wheel must contain exactly one dist-info/entry_points.txt")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(archive.read(entry_names[0]).decode("utf-8"))
        entries = (
            dict(parser.items("console_scripts")) if parser.has_section("console_scripts") else {}
        )
        if entries != CONSOLE_SCRIPTS:
            raise ValueError(f"console entry points mismatch: {entries!r}")

    required = {
        "konwledge/__init__.py",
        "konwledge/__main__.py",
        "konwledge/py.typed",
        *API_MODULES,
        *expected_resources,
    }
    missing = sorted(required.difference(names))
    if missing:
        raise ValueError(f"wheel is missing packaged file: {missing[0]}")
    return Inspection(
        format="wheel",
        checks=(
            "safe_paths",
            "metadata_identity",
            "console_entry_points",
            "package_modules",
            "demo_resources",
            "runtime_exclusions",
        ),
        file_count=len(names),
    )


def inspect_sdist(
    path: Path,
    expected_name: str,
    expected_version: str,
    expected_resources: Sequence[str] = (),
) -> Inspection:
    """Inspect an sdist without extracting untrusted tar members."""

    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = tuple(_safe_member_name(member.name) for member in members if member.isfile())
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("sdist must not contain symbolic or hard links")
        forbidden = _forbidden_members(names, wheel=False)
        if forbidden:
            raise ValueError(f"sdist contains forbidden member: {forbidden[0]}")
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1:
            raise ValueError("sdist must have one top-level directory")
        root = next(iter(roots))
        required = {
            f"{root}/PKG-INFO",
            f"{root}/README.md",
            f"{root}/LICENSE",
            f"{root}/pyproject.toml",
            f"{root}/src/konwledge/__init__.py",
            f"{root}/src/konwledge/py.typed",
            *(f"{root}/src/{module}" for module in API_MODULES),
            *(
                f"{root}/src/{resource}"
                for resource in expected_resources
                if resource.startswith("konwledge/")
            ),
        }
        missing = sorted(required.difference(names))
        if missing:
            raise ValueError(f"sdist is missing source file: {missing[0]}")
        pkg_info = archive.extractfile(f"{root}/PKG-INFO")
        if pkg_info is None:
            raise ValueError("sdist PKG-INFO is unreadable")
        _metadata_identity(pkg_info.read().decode("utf-8"), expected_name, expected_version)

    has_tests = any(
        len(PurePosixPath(name).parts) > 1 and PurePosixPath(name).parts[1] == "tests"
        for name in names
    )
    warnings = (
        ("sdist source tests are expected for Hatch; the wheel excludes them",) if has_tests else ()
    )
    return Inspection(
        format="sdist",
        checks=(
            "safe_paths",
            "metadata_identity",
            "source_package",
            "readme_and_license",
            "runtime_exclusions",
        ),
        file_count=len(names),
        warnings=warnings,
    )


def _sanitized_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "KNOWLEDGE_API_KEY",
    ):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        raise RuntimeError(
            f"command failed with exit {result.returncode}: {rendered}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _venv_python(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not candidate.is_file():
        raise RuntimeError(f"uv did not create the expected venv Python: {candidate}")
    return candidate


def _expected_resource_names() -> tuple[str, ...]:
    corpus = PROJECT_ROOT / "src" / "konwledge" / "demo" / "corpus"
    return tuple(
        sorted(f"konwledge/demo/corpus/{path.name}" for path in corpus.iterdir() if path.is_file())
    )


def _smoke_install(
    *,
    uv: str,
    target_python: str,
    artifact: Path,
    distribution_format: str,
    version: str,
    scratch: Path,
    fixtures: Path,
) -> dict[str, Any]:
    venv = scratch / f"{distribution_format}-venv"
    _run([uv, "venv", "--python", target_python, str(venv)], cwd=scratch)
    python = _venv_python(venv)
    _run([uv, "pip", "install", "--python", str(python), str(artifact)], cwd=scratch)
    run_dir = scratch / f"{distribution_format}-run"
    run_dir.mkdir()
    smoke_script = run_dir / SMOKE_SCRIPT.name
    shutil.copyfile(SMOKE_SCRIPT, smoke_script)
    result = _run(
        [
            str(python),
            "-I",
            str(smoke_script),
            "--expected-version",
            version,
            "--distribution-format",
            distribution_format,
            "--fixtures-dir",
            str(fixtures),
        ],
        cwd=run_dir,
    )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"installed smoke did not emit valid JSON: {result.stdout!r}") from error
    if not isinstance(summary, dict):
        raise RuntimeError("installed smoke summary must be a JSON object")
    # 先证明 core-only 无可选依赖，再在同一隔离环境安装 [api]；不安装 HTTPX/SDK。
    _run([uv, "pip", "install", "--python", str(python), f"{artifact}[api]"], cwd=scratch)
    api_smoke_script = run_dir / API_SMOKE_SCRIPT.name
    shutil.copyfile(API_SMOKE_SCRIPT, api_smoke_script)
    api_result = _run(
        [str(python), "-I", str(api_smoke_script), "--expected-version", version], cwd=run_dir
    )
    api_summary = json.loads(api_result.stdout)
    if not isinstance(api_summary, dict):
        raise RuntimeError("installed API smoke summary must be a JSON object")
    summary["api_extra"] = api_summary
    return summary


def verify(output_dir: Path, target_python: str) -> tuple[Path, Path, tuple[Path, Path]]:
    """Build, inspect, install, and smoke-test both distribution formats."""

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to verify distributions")
    name, version = load_project_metadata()
    normalized = canonicalize_name(name).replace("-", "_")
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir = output_dir / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    _run([target_python, "-c", "import sys; assert sys.version_info >= (3, 11)"], cwd=PROJECT_ROOT)
    _run([uv, "build", "--out-dir", str(build_dir)], cwd=PROJECT_ROOT)

    wheel = build_dir / f"{normalized}-{version}-py3-none-any.whl"
    sdist = build_dir / f"{normalized}-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise RuntimeError(
            f"uv build did not create exact expected artifacts: {wheel.name}, {sdist.name}"
        )
    expected_resources = _expected_resource_names()
    inspections = (
        inspect_wheel(wheel, name, version, expected_resources),
        inspect_sdist(sdist, name, version, expected_resources),
    )
    for inspection in inspections:
        for warning in inspection.warnings:
            print(f"warning: {warning}", file=sys.stderr)

    summary_paths: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="konwledge-isolated-") as temporary:
        scratch = Path(temporary)
        fixtures = scratch / "fixtures"
        fixtures.mkdir()
        for fixture_name in FIXTURE_NAMES:
            shutil.copyfile(PROJECT_ROOT / "examples" / fixture_name, fixtures / fixture_name)
        for artifact, distribution_format in ((wheel, "wheel"), (sdist, "sdist")):
            summary = _smoke_install(
                uv=uv,
                target_python=target_python,
                artifact=artifact,
                distribution_format=distribution_format,
                version=version,
                scratch=scratch,
                fixtures=fixtures,
            )
            summary_path = output_dir / f"{distribution_format}-smoke-summary.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summary_paths.append(summary_path)

    return wheel, sdist, (summary_paths[0], summary_paths[1])


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory that preserves fresh build artifacts and JSON smoke summaries.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Existing Python executable for both isolated venvs (default: current interpreter).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else Path(tempfile.mkdtemp(prefix="konwledge-distribution-")).resolve()
    )
    try:
        wheel, sdist, summaries = verify(output_dir, str(args.python))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified wheel: {wheel}")
    print(f"verified sdist: {sdist}")
    for summary in summaries:
        print(f"smoke summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
