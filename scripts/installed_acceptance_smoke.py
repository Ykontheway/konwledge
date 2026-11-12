"""验证隔离安装的质量 CLI 与自动评测 SDK；不访问模型、SDK 或外部服务。"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import site
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_cli(
    root: Path,
    *arguments: str,
    expected: int = 0,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "konwledge", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"installed CLI returned {result.returncode}, expected {expected}: "
            f"{' '.join(arguments)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def verify_installed_identity(expected_version: str) -> None:
    import konwledge

    require(konwledge.__version__ == expected_version, "installed package version mismatch")
    require(
        importlib.metadata.version("konwledge") == expected_version,
        "installed metadata version mismatch",
    )
    package_file = Path(konwledge.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    installed_paths = tuple(Path(value).resolve() for value in site.getsitepackages())
    require(
        package_file.is_relative_to(prefix)
        and any(package_file.is_relative_to(value) for value in installed_paths),
        "package was not imported from this interpreter's site-packages under sys.prefix",
    )
    for dependency in ("tiktoken", "fastapi", "uvicorn", "psycopg", "openai", "langchain_openai"):
        require(
            importlib.util.find_spec(dependency) is None,
            f"core-only acceptance environment leaked optional dependency: {dependency}",
        )


def verify_quality_cli(root: Path) -> dict[str, object]:
    report_path = root / "quality-acceptance.json"
    process = run_cli(root, "quality", "acceptance", "--report", str(report_path))
    payload = json.loads(process.stdout)
    require(isinstance(payload, dict), "quality CLI output is not an object")
    require(payload.get("passed") is True, "quality acceptance did not pass")
    require(report_path.is_file(), "quality acceptance report was not written")
    require(
        json.loads(report_path.read_text(encoding="utf-8")) == payload, "report differs from stdout"
    )

    quality = payload.get("quality")
    performance = payload.get("performance")
    require(isinstance(quality, dict), "quality report is missing")
    require(isinstance(performance, dict), "performance report is missing")
    dataset = quality.get("dataset")
    require(isinstance(dataset, dict) and dataset.get("case_count") == 30, "expected 30 cases")
    metrics = quality.get("metrics")
    require(
        isinstance(metrics, dict) and metrics.get("case_count") == 30, "metrics case count mismatch"
    )
    require(quality.get("passed") is True, "quality checks failed")

    configuration = performance.get("configuration")
    latency = performance.get("latency_seconds")
    require(isinstance(configuration, dict), "performance configuration is missing")
    require(isinstance(latency, dict), "performance latency is missing")
    require(configuration.get("active_corpus_count") == 1000, "expected 1,000 active units")
    require(configuration.get("total_current_units") == 1001, "expected 1,001 current units")
    require(configuration.get("sample_count") == 30, "expected 30 timed samples")
    require(configuration.get("local_acceptance_threshold_seconds") == 1.5, "P95 limit changed")
    p95 = latency.get("p95")
    require(isinstance(p95, (int, float)) and not isinstance(p95, bool), "P95 is not numeric")
    require(float(p95) <= 1.5 and performance.get("passed") is True, "P95 exceeded 1.5 seconds")

    refused = run_cli(
        root,
        "quality",
        "acceptance",
        "--report",
        str(report_path),
        expected=2,
    )
    require("already exists" in refused.stderr, "acceptance report overwrite guard is unavailable")
    return cast(dict[str, object], payload)


def verify_automatic_evaluation(root: Path) -> dict[str, object]:
    from konwledge import KnowledgeLibrary
    from konwledge.auto_evaluation import AutoEvaluationStatus
    from konwledge.domain import EvaluationItem, EvaluationSet
    from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

    database = root / "automatic-evaluation.db"
    source = root / "installed-automatic-evidence.md"
    source.write_text(
        "# Installed automatic evidence\nThe stable installed audit marker is blue.\n",
        encoding="utf-8",
    )
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    library = KnowledgeLibrary(create_session_factory(engine))
    try:
        first = library.import_path(source)
        require(len(first.imported) == 1, "initial SDK file import failed")
        first_result = first.imported[0].result
        require(first_result.ingestion.created, "initial document was not created")
        require(bool(first_result.units), "initial import produced no knowledge units")
        lineage_id = first_result.units[0].lineage_id

        set_id = uuid4()
        dataset = EvaluationSet(
            id=set_id,
            name="Installed automatic failure audit",
            version=1,
            created_at=datetime.now(UTC),
            items=(
                EvaluationItem(
                    id="intentional-failure",
                    query="definitely-absent-installed-evaluation-token",
                    expected_lineage_ids=frozenset({lineage_id}),
                ),
            ),
        )
        library.evaluation.import_set(dataset)
        config = library.automatic_evaluation.enable(set_id, version=1)
        require(
            config.enabled and config.dataset_id == set_id, "SDK automatic configuration failed"
        )
        require(
            library.automatic_evaluation.history() == (), "enabling unexpectedly ran evaluation"
        )

        unchanged = library.import_path(source).imported[0].result
        require(not unchanged.ingestion.created, "identical real-file import was not idempotent")
        require(not unchanged.units_created, "identical real-file import recreated units")
        require(
            library.automatic_evaluation.history() == (),
            "idempotent import created an automatic evaluation audit",
        )

        source.write_text(
            "# Installed automatic evidence\n"
            "The stable installed audit marker is blue. A real file update keeps its lineage.\n",
            encoding="utf-8",
        )
        updated = library.import_path(source).imported[0].result
        require(
            updated.ingestion.created and updated.units_created, "real file update was not imported"
        )
        jobs = library.automatic_evaluation.history()
        require(len(jobs) == 1, "real update did not create exactly one audit job")
        failed = jobs[0]
        require(
            failed.status is AutoEvaluationStatus.FAILED, "intentional failed audit did not fail"
        )
        require(failed.error_code == "QUALITY_GATE_FAILED", "failed audit error code mismatch")
        require(
            failed.report is not None and failed.report.get("passed") is False,
            "failure report missing",
        )
        require(bool(failed.trigger_revision_ids), "automatic audit has no triggering revisions")
        rendered = json.dumps(failed.report, ensure_ascii=False).casefold()
        require("definitely-absent" not in rendered, "audit report leaked evaluation query")
        require(source.name.casefold() not in rendered, "audit report leaked source path")
        require("stable installed audit marker" not in rendered, "audit report leaked evidence")
        require(
            any("real file update" in unit.text for unit in updated.units),
            "failed evaluation rolled back the committed knowledge update",
        )
    finally:
        engine.dispose()

    status = json.loads(run_cli(root, "auto-evaluation", "status", "-d", str(database)).stdout)
    require(
        status.get("configured") is True and status.get("enabled") is True, "CLI status mismatch"
    )
    manual = run_cli(root, "auto-evaluation", "run", "-d", str(database), expected=3)
    manual_job = json.loads(manual.stdout)
    require(manual_job.get("status") == "failed", "non-passing CLI run did not return failed audit")
    history = json.loads(run_cli(root, "auto-evaluation", "history", "-d", str(database)).stdout)
    require(len(history) == 2, "append-only automatic evaluation history mismatch")
    require(history[0]["id"] == manual_job["id"], "manual audit was not appended")
    return {
        "configured": status["configured"],
        "update_audit_status": history[1]["status"],
        "manual_exit": 3,
        "history_count": len(history),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    try:
        verify_installed_identity(args.expected_version)
        with tempfile.TemporaryDirectory(prefix="konwledge-installed-acceptance-") as temporary:
            root = Path(temporary)
            quality = verify_quality_cli(root)
            automatic = verify_automatic_evaluation(root)
    except (
        AssertionError,
        ImportError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"installed acceptance smoke failed: {error}", file=sys.stderr)
        return 1

    performance = quality["performance"]
    assert isinstance(performance, dict)
    latency = performance["latency_seconds"]
    assert isinstance(latency, dict)
    print(
        json.dumps(
            {
                "version": args.expected_version,
                "passed": True,
                "checks": [
                    "installed_site_packages_identity",
                    "core_optional_dependency_boundary",
                    "quality_cli_30_cases",
                    "quality_cli_1000_active_units",
                    "quality_performance_p95",
                    "quality_report_overwrite_guard",
                    "automatic_evaluation_sdk_configuration",
                    "real_file_import_update_idempotency",
                    "failed_audit_privacy_and_commit_safety",
                    "automatic_evaluation_cli_exit_3",
                ],
                "quality_cases": 30,
                "active_units": 1000,
                "p95_seconds": latency["p95"],
                "automatic_evaluation": automatic,
                "external_model_or_sdk_requests": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
