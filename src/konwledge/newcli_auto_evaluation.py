"""Typer commands for opt-in automatic knowledge evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID

import typer

from konwledge.retrieval import RetrievalMode


def _helpers() -> tuple[Path, Any, Any]:
    # Delayed import is required because cli.py registers this app.
    from konwledge.cli import DEFAULT_DATABASE, _fail, _open_scoped_library

    return DEFAULT_DATABASE, _fail, _open_scoped_library


def _service(library: Any) -> Any:
    return library.automatic_evaluation


def _fail_for(error: Exception) -> NoReturn:
    _, fail, _ = _helpers()
    from konwledge.auto_evaluation import AutoEvaluationNotFoundError

    if isinstance(error, AutoEvaluationNotFoundError):
        fail(str(error))
        raise AssertionError("unreachable")
    if isinstance(error, (ValueError, TypeError)):
        fail(str(error), code=2)
        raise AssertionError("unreachable")
    fail("automatic evaluation operation failed")
    raise AssertionError("unreachable")


def _load_model(path: Path | None, model: type[Any], label: str) -> Any | None:
    if path is None:
        return None
    _, fail, _ = _helpers()
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        fail(f"invalid {label} JSON", code=2)
        raise AssertionError("unreachable") from None


def _config_payload(config: Any | None) -> dict[str, object]:
    if config is None:
        return {"configured": False, "enabled": False}
    return {
        "configured": True,
        "enabled": config.enabled,
        "generation": config.generation,
        "dataset": {
            "id": str(config.dataset_id),
            "version": config.dataset_version,
            "hash": config.dataset_hash,
        },
        "quality_policy": config.quality_policy.model_dump(mode="json"),
        "has_baseline": config.baseline is not None,
        "has_regression_policy": config.regression_policy is not None,
        "retrieval": {
            "mode": config.mode.value,
            "top_k": config.top_k,
            "candidate_limit": config.candidate_limit,
            "rrf_k": config.rrf_k,
        },
        "updated_at": config.updated_at.isoformat(),
    }


def _job_payload(job: Any) -> dict[str, object]:
    return {
        "id": str(job.id),
        "parent_job_id": None if job.parent_job_id is None else str(job.parent_job_id),
        "status": job.status.value,
        "config_generation": job.config_generation,
        "dataset": {
            "id": str(job.dataset_id),
            "version": job.dataset_version,
            "hash": job.dataset_hash,
        },
        "knowledge_fingerprint": job.knowledge_fingerprint,
        "trigger_revision_ids": [str(value) for value in job.trigger_revision_ids],
        "created_at": job.created_at.isoformat(),
        "evaluated_at": job.evaluated_at.isoformat(),
        "started_at": None if job.started_at is None else job.started_at.isoformat(),
        "finished_at": None if job.finished_at is None else job.finished_at.isoformat(),
        "error_code": job.error_code,
        "attempt_count": job.attempt_count,
        "recovery_errors": list(job.recovery_errors),
        "report": job.report,
    }


def _print(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


auto_evaluation_app = typer.Typer(
    name="auto-evaluation",
    help="Opt in to crash-safe quality checks after committed knowledge changes.",
    no_args_is_help=True,
)


@auto_evaluation_app.command("enable")
def enable_command(
    set_id: Annotated[UUID, typer.Option("--set-id", help="Pinned evaluation set UUID.")],
    version: Annotated[int, typer.Option("--version", min=1, help="Pinned immutable version.")],
    policy_json: Annotated[Path | None, typer.Option("--policy-json")] = None,
    baseline_json: Annotated[Path | None, typer.Option("--baseline-json")] = None,
    regression_policy_json: Annotated[Path | None, typer.Option("--regression-policy-json")] = None,
    mode: Annotated[RetrievalMode, typer.Option("--mode")] = RetrievalMode.LEXICAL,
    top_k: Annotated[int, typer.Option("--top-k", min=1)] = 5,
    candidate_limit: Annotated[int | None, typer.Option("--candidate-limit", min=1)] = None,
    rrf_k: Annotated[int, typer.Option("--rrf-k", min=1)] = 60,
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    """Persist an exact opt-in pin; enabling never evaluates or promotes current knowledge."""

    # Strict type validation for CLI inputs
    if not isinstance(version, int) or isinstance(version, bool):
        _fail_for(TypeError("version must be an integer"))
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        _fail_for(TypeError("top-k must be an integer"))
    if not isinstance(rrf_k, int) or isinstance(rrf_k, bool):
        _fail_for(TypeError("rrf-k must be an integer"))
    if candidate_limit is not None and (
        not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool)
    ):
        _fail_for(TypeError("candidate-limit must be an integer"))

    default, _, open_library = _helpers()
    from konwledge.evaluation import EvaluationBaseline, GatePolicy
    from konwledge.quality import QualityPolicy

    policy = _load_model(policy_json, QualityPolicy, "quality policy")
    baseline = _load_model(baseline_json, EvaluationBaseline, "evaluation baseline")
    regression = _load_model(regression_policy_json, GatePolicy, "regression policy")
    try:
        with open_library(database or default) as library:
            config = _service(library).enable(
                set_id,
                version=version,
                policy=policy,
                baseline=baseline,
                regression_policy=regression,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
            )
    except Exception as error:
        _fail_for(error)
    _print(_config_payload(config))


@auto_evaluation_app.command("disable")
def disable_command(
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            config = _service(library).disable()
    except Exception as error:
        _fail_for(error)
    _print(_config_payload(config))


@auto_evaluation_app.command("status")
def status_command(
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            config = _service(library).status()
    except Exception as error:
        _fail_for(error)
    _print(_config_payload(config))


def _exit_for_job(job: Any) -> None:
    _print(_job_payload(job))
    if job.status.value != "passed":
        raise typer.Exit(code=3)


@auto_evaluation_app.command("run")
def run_command(
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    """Append and execute one manual job; non-passing outcomes exit with status 3."""

    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            job = _service(library).run()
    except Exception as error:
        _fail_for(error)
    _exit_for_job(job)


@auto_evaluation_app.command("history")
def history_command(
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool):
        _fail_for(TypeError("limit must be an integer"))
    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            jobs = _service(library).history(limit=limit)
    except Exception as error:
        _fail_for(error)
    _print([_job_payload(job) for job in jobs])


@auto_evaluation_app.command("show")
def show_command(
    job_id: Annotated[UUID, typer.Argument(help="Automatic evaluation job UUID.")],
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            job = _service(library).show(job_id)
    except Exception as error:
        _fail_for(error)
    _print(_job_payload(job))


@auto_evaluation_app.command("retry")
def retry_command(
    job_id: Annotated[UUID, typer.Argument(help="Failed, blocked, or superseded job UUID.")],
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    """Append a retry job and execute it; history is never overwritten."""

    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            service = _service(library)
            retry = service.retry(job_id)
            service.drain()
            job = service.show(retry.id)
    except Exception as error:
        _fail_for(error)
    _exit_for_job(job)


@auto_evaluation_app.command("recover")
def recover_command(
    drain: Annotated[bool, typer.Option("--drain/--no-drain")] = True,
    database: Annotated[Path | None, typer.Option("--database", "-d")] = None,
) -> None:
    """Invalidate expired worker leases, optionally draining recovered jobs."""

    default, _, open_library = _helpers()
    try:
        with open_library(database or default) as library:
            service = _service(library)
            recovered = service.recover()
            jobs = service.drain() if drain else ()
    except Exception as error:
        _fail_for(error)
    _print({"recovered": recovered, "drained": [_job_payload(job) for job in jobs]})
