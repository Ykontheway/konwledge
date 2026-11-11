"""Smoke-test one installed Konwledge distribution from an isolated interpreter."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import site
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from konwledge import KnowledgeLibrary


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sanitized_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    expected: int = 0,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=sanitized_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"installed command returned {result.returncode}, expected {expected}: "
            f"{Path(command[0]).name} {' '.join(command[1:])}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def console_script(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = Path(sys.executable).parent / f"{name}{suffix}"
    require(path.is_file(), f"installed console script is missing: {name}")
    return path


def cli(
    executable: Path,
    cwd: Path,
    *arguments: str,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    return run([str(executable), *arguments], cwd=cwd, expected=expected)


def open_library(database: Path, *, at: datetime | None = None) -> tuple[Engine, KnowledgeLibrary]:
    from konwledge import KnowledgeLibrary
    from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    clock = None if at is None else lambda: at
    library = KnowledgeLibrary(create_session_factory(engine), clock=clock)
    return engine, library


def is_installed_location(package_file: Path, prefix: Path, site_packages: Sequence[Path]) -> bool:
    """Return whether a package file belongs to this interpreter's installed paths."""

    resolved = package_file.resolve()
    return resolved.is_relative_to(prefix.resolve()) and any(
        resolved.is_relative_to(path.resolve()) for path in site_packages
    )


def verify_installed_identity(expected_version: str) -> None:
    import konwledge

    require("konwledge.application" not in sys.modules, "root package eagerly imported application")
    from konwledge import KnowledgeLibrary

    require(KnowledgeLibrary.__name__ == "KnowledgeLibrary", "root lazy API is unavailable")
    require(konwledge.__version__ == expected_version, "root version does not match")
    require(
        importlib.metadata.version("konwledge") == expected_version,
        "installed metadata version does not match",
    )
    package_file = Path(konwledge.__file__)
    prefixes = [Path(path) for path in site.getsitepackages()]
    require(
        is_installed_location(package_file, Path(sys.prefix), prefixes),
        "package was not imported from this environment's site-packages under sys.prefix",
    )


def verify_cli_aliases(run_dir: Path, expected_version: str) -> tuple[Path, Path]:
    knowledge = console_script("knowledge")
    konwledge = console_script("konwledge")
    expected = f"knowledge {expected_version}"
    for executable in (knowledge, konwledge):
        require(
            cli(executable, run_dir, "--version").stdout.strip() == expected, "CLI version mismatch"
        )
        help_result = cli(executable, run_dir, "--help")
        require(
            "Import and inspect traceable local knowledge" in help_result.stdout,
            "CLI help mismatch",
        )
    module_version = run([sys.executable, "-I", "-m", "konwledge", "--version"], cwd=run_dir)
    require(module_version.stdout.strip() == expected, "module CLI version mismatch")
    module_help = run([sys.executable, "-I", "-m", "konwledge", "--help"], cwd=run_dir)
    require(
        "Import and inspect traceable local knowledge" in module_help.stdout, "module help mismatch"
    )
    return knowledge, konwledge


def verify_resources_and_demo(knowledge: Path, root: Path) -> Path:
    corpus = resources.files("konwledge.demo").joinpath("corpus")
    expected = {
        "deployment-guide.v1.md",
        "deployment-guide.v2.md",
        "deployment-guide.v3.md",
        "oncall-faq.v1.md",
    }
    actual = {entry.name for entry in corpus.iterdir() if entry.is_file()}
    require(actual == expected, "installed demo corpus is incomplete")
    require(
        all(corpus.joinpath(name).read_text(encoding="utf-8").strip() for name in expected),
        "empty demo resource",
    )

    workspace = root / "demo"
    built = cli(knowledge, root, "demo", "build", "--workspace", str(workspace))
    timeline = [line for line in built.stdout.splitlines() if " | " in line]
    require(len(timeline) == 8, "demo did not execute its full eight-step timeline")
    database = workspace / "knowledge.db"
    require(database.is_file(), "demo database was not created")
    return database


def verify_plaintext_context(knowledge: Path, root: Path) -> None:
    from konwledge.context import ContextPacket

    source = root / "plain-evidence.md"
    text = '# Installed evidence\nThe service says "exact-token" on port 9191.\n'
    source.write_text(text, encoding="utf-8")
    database = root / "plain.db"
    engine, library = open_library(database)
    try:
        report = library.import_path(source)
        require(len(report.imported) == 1, "SDK plaintext import failed")
        search = library.search("exact-token")
        require(
            len(search.hits) == 1 and search.hits[0].unit.text == text,
            "SDK plaintext search failed",
        )
        packet = library.build_context("exact-token", max_chars=4000, max_units=1)
        require(len(packet.blocks) == 1, "SDK context did not return exact evidence")
        block = packet.blocks[0]
        require(block.citation.quote == text, "SDK citation did not preserve the exact quote")
        require(
            text[block.citation.start_char : block.citation.end_char] == block.citation.quote,
            "citation offsets are invalid",
        )
        unit_id = block.citation.knowledge_unit_id
    finally:
        engine.dispose()

    output = cli(
        knowledge,
        root,
        "context",
        "exact-token",
        "--max-chars",
        "4000",
        "--max-units",
        "1",
        "--json",
        "-d",
        str(database),
    )
    parsed = ContextPacket.model_validate_json(output.stdout)
    require(parsed.blocks[0].citation.quote == text, "CLI context quote differs from SDK")
    limited = cli(
        knowledge,
        root,
        "context",
        "exact-token",
        "--max-chars",
        "0",
        "--json",
        "-d",
        str(database),
    )
    require(
        not ContextPacket.model_validate_json(limited.stdout).blocks, "context budget was ignored"
    )
    cli(
        knowledge,
        root,
        "stale",
        str(unit_id),
        "--reason",
        "distribution smoke",
        "-d",
        str(database),
    )
    excluded = cli(knowledge, root, "context", "exact-token", "--json", "-d", str(database))
    require(
        not ContextPacket.model_validate_json(excluded.stdout).blocks,
        "stale evidence was not filtered",
    )
    included = cli(
        knowledge,
        root,
        "context",
        "exact-token",
        "--include-stale",
        "--json",
        "-d",
        str(database),
    )
    require(
        len(ContextPacket.model_validate_json(included.stdout).blocks) == 1,
        "stale evidence opt-in failed",
    )


def verify_demo_retrieval(database: Path) -> None:
    from konwledge.domain import KnowledgeUnitStatus

    at = datetime(2026, 11, 10, tzinfo=UTC)
    historical_at = datetime.fromisoformat("2026-10-10T12:00:00+08:00")
    engine, library = open_library(database, at=at)
    try:
        summaries = library.list_documents()
        guide = next(
            item
            for item in summaries
            if item.source.title == "deployment-guide.md" and item.is_head
        )
        faq = next(
            item for item in summaries if item.source.title == "oncall-faq.md" and item.is_head
        )
        current = library.search("监听", source_id=guide.source.id)
        require(
            bool(current.hits) and "8080" in current.hits[0].unit.text, "current demo query failed"
        )
        historical = library.search("监听", source_id=guide.source.id, at=historical_at)
        require(
            bool(historical.hits) and "8000" in historical.hits[0].unit.text,
            "historical demo query failed",
        )
        require(
            not library.search("监听", source_id=faq.source.id).hits, "stale FAQ was not filtered"
        )
        stale = library.search("监听", source_id=faq.source.id, include_stale=True)
        require(
            bool(stale.hits) and stale.hits[0].status is KnowledgeUnitStatus.STALE,
            "stale FAQ opt-in failed",
        )
    finally:
        engine.dispose()


def verify_evaluation(
    knowledge: Path,
    database: Path,
    fixtures: Path,
    root: Path,
) -> tuple[int, int]:
    from konwledge.evaluation import (
        EvaluationBaseline,
        EvaluationComparison,
        EvaluationReport,
        EvaluationSet,
        GatePolicy,
        GateResult,
        compare_baseline,
        evaluate_gate,
    )

    dataset_path = fixtures / "evaluation-demo.json"
    committed_baseline = fixtures / "evaluation-baseline.json"
    policy_path = fixtures / "evaluation-gate.json"
    baseline_before = committed_baseline.read_bytes()
    dataset = EvaluationSet.model_validate_json(dataset_path.read_bytes())
    baseline = EvaluationBaseline.model_validate_json(baseline_before)
    policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    at = datetime(2026, 11, 10, tzinfo=UTC)

    engine, library = open_library(database, at=at)
    try:
        imported = library.evaluation.import_set(dataset)
        require(imported.dataset == dataset, "SDK evaluation dataset import failed")
        healthy = library.evaluation.run(
            dataset.id,
            version=1,
            top_k=5,
            candidate_limit=50,
            rrf_k=60,
            at=at,
        )
        sdk_gate = evaluate_gate(compare_baseline(baseline, healthy), policy)
        require(sdk_gate.passed, "healthy SDK report failed committed gate")
    finally:
        engine.dispose()

    report_path = root / "healthy-report.json"
    report_path.write_text(healthy.model_dump_json(indent=2) + "\n", encoding="utf-8")
    generated_baseline = root / "generated-baseline.json"
    cli(
        knowledge,
        root,
        "evaluation",
        "baseline",
        str(report_path),
        "--output",
        str(generated_baseline),
        "--json",
    )
    generated_before = generated_baseline.read_bytes()
    require(generated_before == baseline_before, "generated baseline differs from approved fixture")
    refused = cli(
        knowledge,
        root,
        "evaluation",
        "baseline",
        str(report_path),
        "--output",
        str(generated_baseline),
        expected=2,
    )
    require("--overwrite" in refused.stderr, "baseline overwrite guard diagnostic is missing")
    require(generated_baseline.read_bytes() == generated_before, "overwrite guard changed baseline")

    comparison = cli(
        knowledge,
        root,
        "evaluation",
        "compare",
        str(committed_baseline),
        str(report_path),
        "--json",
    )
    require(
        not EvaluationComparison.model_validate_json(comparison.stdout).degraded,
        "healthy comparison degraded",
    )
    passed = cli(
        knowledge,
        root,
        "evaluation",
        "gate",
        str(committed_baseline),
        str(report_path),
        "--policy",
        str(policy_path),
        "--json",
    )
    healthy_gate = GateResult.model_validate_json(passed.stdout)
    require(healthy_gate.passed, "healthy installed CLI gate failed")

    current_case = next(case for case in healthy.results if case.item_id == "guide-port-current")
    require(len(current_case.retrieval.hits) == 1, "healthy current case has unexpected hits")
    engine, stale_library = open_library(database, at=at - timedelta(seconds=1))
    try:
        stale_library.lifecycle.mark_stale(
            current_case.retrieval.hits[0].unit.id,
            reason="distribution regression smoke",
        )
    finally:
        engine.dispose()

    candidate_process = cli(
        knowledge,
        root,
        "evaluation",
        "run",
        str(dataset.id),
        "--version",
        "1",
        "--mode",
        "lexical",
        "--top-k",
        "5",
        "--candidate-limit",
        "50",
        "--rrf-k",
        "60",
        "--at",
        "2026-11-10T00:00:00Z",
        "-d",
        str(database),
        "--json",
    )
    candidate = EvaluationReport.model_validate_json(candidate_process.stdout)
    failed_case = next(case for case in candidate.results if case.item_id == "guide-port-current")
    require(not failed_case.retrieval.hits, "real stale change did not regress current case")
    candidate_path = root / "stale-candidate.json"
    candidate_path.write_text(candidate_process.stdout, encoding="utf-8")
    blocked = cli(
        knowledge,
        root,
        "evaluation",
        "gate",
        str(committed_baseline),
        str(candidate_path),
        "--policy",
        str(policy_path),
        "--json",
        expected=3,
    )
    blocked_gate = GateResult.model_validate_json(blocked.stdout)
    require(
        not blocked_gate.passed and bool(blocked_gate.violations), "regression gate did not block"
    )
    require(committed_baseline.read_bytes() == baseline_before, "fixture baseline was modified")
    return len(healthy.results), len(healthy_gate.checks)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--distribution-format", choices=("wheel", "sdist"), required=True)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        required=True,
        help="External evaluation test inputs copied by the orchestrator, not package resources.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    checks: list[str] = []
    try:
        verify_installed_identity(args.expected_version)
        checks.append("installed_identity")
        with tempfile.TemporaryDirectory(prefix="konwledge-smoke-") as temporary:
            root = Path(temporary)
            knowledge, _ = verify_cli_aliases(root, args.expected_version)
            checks.append("cli_aliases")
            database = verify_resources_and_demo(knowledge, root)
            checks.extend(("packaged_resources", "demo_timeline"))
            verify_plaintext_context(knowledge, root)
            checks.append("context_and_stale_filtering")
            verify_demo_retrieval(database)
            checks.append("historical_demo_retrieval")
            cases, gate_checks = verify_evaluation(
                knowledge,
                database,
                args.fixtures_dir.resolve(),
                root,
            )
            checks.append("evaluation_baseline_gate")
    except (AssertionError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"installed smoke failed: {error}", file=sys.stderr)
        return 1

    summary = {
        "version": args.expected_version,
        "distribution_format": args.distribution_format,
        "check_names": checks,
        "counts": {
            "checks": len(checks),
            "demo_steps": 8,
            "evaluation_cases": cases,
            "gate_checks": gate_checks,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
