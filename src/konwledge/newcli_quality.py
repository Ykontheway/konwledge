"""Quality acceptance and performance benchmark CLI commands."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn, Protocol, cast
from uuid import UUID

import typer
from pydantic import ValidationError

from konwledge.quality import QualityPolicy, QualityReport, QualityService
from konwledge.retrieval import RetrievalMode


class _QualityLibrary(Protocol):
    @property
    def quality(self) -> QualityService: ...


class _Fail(Protocol):
    def __call__(self, message: str, *, code: int = 1) -> NoReturn: ...


class _AtomicWrite(Protocol):
    def __call__(self, path: Path, content: str, *, overwrite: bool) -> None: ...


_OpenScopedLibrary = Callable[[Path], AbstractContextManager[_QualityLibrary]]
_ParseTime = Callable[[str | None], datetime | None]
_CliHelpers = tuple[Path, _Fail, _OpenScopedLibrary, _ParseTime, _AtomicWrite]


def _get_cli_helpers() -> _CliHelpers:
    # Delayed import avoids a cycle while cli.py registers this Typer group.
    from konwledge.cli import (
        DEFAULT_DATABASE,
        _atomic_write,
        _fail,
        _open_scoped_library,
        _parse_time,
    )

    return (
        DEFAULT_DATABASE,
        _fail,
        cast(_OpenScopedLibrary, _open_scoped_library),
        _parse_time,
        _atomic_write,
    )


quality_app = typer.Typer(
    name="quality",
    help="Quality acceptance and performance benchmarks.",
    no_args_is_help=True,
)


def _compact_quality_payload(report: QualityReport) -> dict[str, object]:
    return {
        "passed": report.passed,
        "dataset": {
            "id": str(report.baseline.dataset.id),
            "version": report.baseline.dataset.version,
            "hash": report.baseline.dataset_hash,
            "case_count": report.metrics.case_count,
        },
        "metrics": report.metrics.model_dump(mode="json"),
        "citation_coverage": report.citation_coverage,
        "stale_hits": report.stale_hits,
        "degraded": report.degraded,
        "checks": [check.model_dump(mode="json") for check in report.checks],
        "violations": list(report.violations),
        "citation_scope": "retrieval evidence citable; not generated-answer correctness",
    }


def _render(payload: dict[str, object]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def _write_new_report(
    report_path: Path,
    rendered: str,
    *,
    fail: _Fail,
    atomic_write: _AtomicWrite,
) -> None:
    if report_path.exists():
        fail("report output already exists; choose a new path", code=2)
    try:
        atomic_write(report_path, rendered, overwrite=False)
    except FileExistsError:
        fail("report output already exists; choose a new path", code=2)
    except OSError as error:
        fail(f"could not write report output: {error}")


@quality_app.command("run")
def run_command(
    set_id: Annotated[UUID, typer.Option("--set-id", help="Evaluation set UUID.")],
    version: Annotated[
        int | None, typer.Option("--version", min=1, help="Evaluation set version.")
    ] = None,
    database: Annotated[
        Path | None, typer.Option("--database", "-d", help="Metadata database file.")
    ] = None,
    policy_json: Annotated[
        Path | None, typer.Option("--policy-json", help="Quality policy JSON file.")
    ] = None,
    baseline_json: Annotated[
        Path | None, typer.Option("--baseline-json", help="Baseline JSON file.")
    ] = None,
    regression_policy_json: Annotated[
        Path | None,
        typer.Option("--regression-policy-json", help="Regression gate policy JSON file."),
    ] = None,
    mode: Annotated[RetrievalMode, typer.Option("--mode")] = RetrievalMode.LEXICAL,
    top_k: Annotated[int, typer.Option("--top-k", min=1)] = 5,
    candidate_limit: Annotated[
        int | None, typer.Option("--candidate-limit", min=1, help="Per-route candidate limit.")
    ] = None,
    rrf_k: Annotated[int, typer.Option("--rrf-k", min=1, help="RRF rank constant.")] = 60,
    at: Annotated[str | None, typer.Option("--at", help="Evaluation time.")] = None,
    report_path: Annotated[
        Path | None, typer.Option("--report", help="Write a new report JSON file.")
    ] = None,
) -> None:
    """Run quality acceptance for one evaluation set version."""

    default_database, fail, open_library, parse_time, atomic_write = _get_cli_helpers()
    db_path = database or default_database
    moment = parse_time(at)
    if report_path is not None and report_path.exists():
        fail("report output already exists; choose a new path", code=2)

    from konwledge.evaluation import EvaluationBaseline, GatePolicy

    policy: QualityPolicy | None = None
    if policy_json is not None:
        try:
            policy = QualityPolicy.model_validate_json(policy_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as error:
            fail(f"failed to load quality policy: {error}", code=2)

    baseline: EvaluationBaseline | None = None
    if baseline_json is not None:
        try:
            baseline = EvaluationBaseline.model_validate_json(
                baseline_json.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as error:
            fail(f"failed to load baseline: {error}", code=2)

    regression_policy: GatePolicy | None = None
    if regression_policy_json is not None:
        try:
            regression_policy = GatePolicy.model_validate_json(
                regression_policy_json.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as error:
            fail(f"failed to load regression policy: {error}", code=2)

    try:
        with open_library(db_path) as library:
            report = library.quality.run(
                set_id,
                version=version,
                policy=policy,
                baseline=baseline,
                regression_policy=regression_policy,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                at=moment,
            )
    except (LookupError, OSError, RuntimeError, ValidationError, ValueError) as error:
        fail(str(error), code=2 if isinstance(error, (ValidationError, ValueError)) else 1)

    if report_path is not None:
        _write_new_report(
            report_path,
            _render(_compact_quality_payload(report)),
            fail=fail,
            atomic_write=atomic_write,
        )

    for violation in report.violations:
        typer.echo(f"Violation: {violation}", err=True)
    if not report.passed:
        typer.echo("Quality acceptance FAILED")
        raise typer.Exit(code=1)
    typer.echo("Quality acceptance PASSED")


@quality_app.command("acceptance")
def acceptance_command(
    report_path: Annotated[
        Path | None,
        typer.Option("--report", help="Opt-in new compact acceptance report path."),
    ] = None,
) -> None:
    """Build the installed 30-case/1000-active-unit corpus and run full acceptance."""

    _, fail, _, _, atomic_write = _get_cli_helpers()
    if report_path is not None and report_path.exists():
        fail("report output already exists; choose a new path", code=2)

    from konwledge.quality.acceptance import populate_corpus, run_performance, run_quality
    from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

    try:
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
    except (OSError, RuntimeError, ValidationError, ValueError) as error:
        fail(f"acceptance execution failed: {error}")

    overall_passed = quality.passed and performance.passed
    payload: dict[str, object] = {
        "passed": overall_passed,
        "quality": _compact_quality_payload(quality),
        "performance": performance.as_dict(),
    }
    rendered = _render(payload)
    typer.echo(rendered, nl=False)
    if report_path is not None:
        _write_new_report(
            report_path,
            rendered,
            fail=fail,
            atomic_write=atomic_write,
        )
    if not overall_passed:
        raise typer.Exit(code=1)
