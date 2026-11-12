#!/usr/bin/env python3
"""CI-direct entry point for the installed quality acceptance contract."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from konwledge.cli import _atomic_write
from konwledge.quality import QualityReport
from konwledge.quality.acceptance import (
    PerformanceResult,
    populate_corpus,
    run_performance,
    run_quality,
)
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine


def _payload(quality: QualityReport, performance: PerformanceResult) -> dict[str, object]:
    return {
        "passed": quality.passed and performance.passed,
        "quality": {
            "passed": quality.passed,
            "dataset": {
                "id": str(quality.baseline.dataset.id),
                "version": quality.baseline.dataset.version,
                "hash": quality.baseline.dataset_hash,
                "case_count": quality.metrics.case_count,
            },
            "metrics": quality.metrics.model_dump(mode="json"),
            "citation_coverage": quality.citation_coverage,
            "stale_hits": quality.stale_hits,
            "degraded": quality.degraded,
            "checks": [check.model_dump(mode="json") for check in quality.checks],
            "violations": list(quality.violations),
            "citation_scope": (
                "retrieval evidence citable only; not generated-answer factual correctness"
            ),
        },
        "performance": performance.as_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        help="Opt-in new path for the compact report; existing files are never replaced.",
    )
    arguments = parser.parse_args()

    try:
        if arguments.report is not None and arguments.report.exists():
            raise FileExistsError("report output already exists; choose a new path")
        with tempfile.TemporaryDirectory(prefix="konwledge-quality-") as directory:
            database = Path(directory) / "acceptance.db"
            engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
            try:
                create_schema(engine)
                session_factory = create_session_factory(engine)
                populate_corpus(session_factory)
                quality = run_quality(session_factory)
                performance = run_performance(session_factory)
            finally:
                engine.dispose()
        payload = _payload(quality, performance)
        rendered = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        if arguments.report is not None:
            if arguments.report.exists():
                raise FileExistsError("report output already exists; choose a new path")
            _atomic_write(arguments.report, rendered, overwrite=False)
    except (OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {"passed": False, "error_type": type(error).__name__, "error": str(error)},
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2

    print(rendered, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
