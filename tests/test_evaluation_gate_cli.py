"""U16 file-only evaluation baseline, comparison, and gate CLI acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner, Result

from konwledge import cli
from konwledge.application import KnowledgeLibrary
from konwledge.domain import EvaluationItem
from konwledge.evaluation import (
    EvaluationBaseline,
    EvaluationComparison,
    EvaluationReport,
    EvaluationSet,
    GatePolicy,
    GateResult,
    GateRule,
    MetricName,
    aggregate_metrics,
    dump_evaluation_set,
    dump_gate_policy,
    score_ranked_lineages,
    validate_evaluation_report,
)
from konwledge.retrieval import RetrievalMode
from konwledge.storage import create_session_factory, create_sqlite_engine

runner = CliRunner()


def invoke(*arguments: str) -> Result:
    return runner.invoke(cli.app, list(arguments))


@pytest.fixture
def report_file(tmp_path: Path) -> Path:
    """Create a complete report through a real imported corpus and KnowledgeLibrary."""

    database = tmp_path / "真实 corpus" / "knowledge.db"
    source = tmp_path / "语料 with spaces.md"
    source.write_text("# Guide\nservice listens on the target port.\n", encoding="utf-8")
    imported = invoke("import", str(source), "-d", str(database))
    assert imported.exit_code == 0, imported.stderr

    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    library = KnowledgeLibrary(create_session_factory(engine))
    summary = library.list_documents()[0]
    dataset = EvaluationSet(
        id=uuid4(),
        name="U16 CLI 中文",
        version=1,
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
        items=(
            EvaluationItem(
                id="answerable",
                query="target",
                expected_lineage_ids=frozenset({summary.units[0].lineage_id}),
                source_id=summary.source.id,
            ),
            EvaluationItem(
                id="no-answer",
                query="definitely-absent-token",
                expect_no_answer=True,
                source_id=summary.source.id,
            ),
        ),
    )
    engine.dispose()

    dataset_path = tmp_path / "evaluation 数据集.json"
    dataset_path.write_text(dump_evaluation_set(dataset), encoding="utf-8")
    added = invoke("evaluation", "import", str(dataset_path), "-d", str(database))
    assert added.exit_code == 0, added.stderr
    executed = invoke(
        "evaluation",
        "run",
        str(dataset.id),
        "--version",
        "1",
        "--top-k",
        "3",
        "--json",
        "-d",
        str(database),
    )
    assert executed.exit_code == 0, executed.stderr
    report = EvaluationReport.model_validate_json(executed.stdout)
    assert report.metrics.mean_recall == 1.0

    path = tmp_path / "approved report 中文.json"
    path.write_text(executed.stdout, encoding="utf-8")
    return path


def forbid_library(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("file-only evaluation command opened the knowledge library")

    monkeypatch.setattr(cli, "_open_library", explode)


def write_regression(source: Path, destination: Path) -> EvaluationReport:
    report = EvaluationReport.model_validate_json(source.read_text(encoding="utf-8"))
    first = report.results[0]
    metrics = score_ranked_lineages(
        report.dataset.items[0].expected_lineage_ids,
        (),
        k=report.top_k,
    )
    changed = first.model_copy(
        update={
            "retrieval": first.retrieval.model_copy(update={"hits": ()}),
            "metrics": metrics,
        }
    )
    cases = (changed, *report.results[1:])
    candidate = report.model_copy(
        update={"results": cases, "metrics": aggregate_metrics(tuple(c.metrics for c in cases))}
    )
    candidate = validate_evaluation_report(candidate)
    destination.write_text(candidate.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return candidate


def test_file_only_baseline_compare_and_gate_json(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    baseline_path = tmp_path / "输出 directory 中文" / "approved baseline.json"

    created = invoke(
        "evaluation",
        "baseline",
        str(report_file),
        "--output",
        str(baseline_path),
        "--json",
    )
    assert created.exit_code == 0, created.stderr
    assert created.stderr == ""
    baseline = EvaluationBaseline.model_validate_json(created.stdout)
    assert (
        EvaluationBaseline.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        == baseline
    )
    assert report_file.exists()
    assert baseline_path.stat().st_mode & 0o777 == 0o644
    assert not list(baseline_path.parent.glob("*.tmp"))

    compared = invoke("evaluation", "compare", str(baseline_path), str(report_file), "--json")
    assert compared.exit_code == 0 and compared.stderr == ""
    comparison = EvaluationComparison.model_validate_json(compared.stdout)
    assert all(delta.delta in (0.0, None) for delta in comparison.deltas)

    gated = invoke("evaluation", "gate", str(baseline_path), str(report_file), "--json")
    assert gated.exit_code == 0 and gated.stderr == ""
    result = GateResult.model_validate_json(gated.stdout)
    assert result.passed and all(check.passed in (True, None) for check in result.checks)


def test_regression_is_valid_comparison_but_gate_exit_three_with_pure_json(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    baseline_path = tmp_path / "baseline.json"
    assert (
        invoke("evaluation", "baseline", str(report_file), "--output", str(baseline_path)).exit_code
        == 0
    )
    candidate_path = tmp_path / "regression.json"
    write_regression(report_file, candidate_path)

    compared = invoke("evaluation", "compare", str(baseline_path), str(candidate_path), "--json")
    assert compared.exit_code == 0
    assert any(
        delta.delta == -1.0
        for delta in EvaluationComparison.model_validate_json(compared.stdout).deltas
    )

    gated = invoke("evaluation", "gate", str(baseline_path), str(candidate_path), "--json")
    assert gated.exit_code == 3
    result = GateResult.model_validate_json(gated.stdout)
    assert result.passed is False
    assert any("case answerable" in violation for violation in result.violations)
    assert "Violation:" in gated.stderr

    human = invoke("evaluation", "gate", str(baseline_path), str(candidate_path))
    assert human.exit_code == 3
    assert "Gate: BLOCKED" in human.stdout and "case answerable" in human.stdout
    assert "Violation:" in human.stderr
    assert cli.main(["evaluation", "gate", str(baseline_path), str(candidate_path), "--json"]) == 3


def test_overwrite_is_explicit_and_validation_precedes_output_creation(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    output = tmp_path / "baseline.json"
    first = invoke("evaluation", "baseline", str(report_file), "--output", str(output))
    assert first.exit_code == 0
    original = output.read_bytes()

    refused = invoke("evaluation", "baseline", str(report_file), "--output", str(output))
    assert refused.exit_code == 2 and refused.stdout == ""
    assert "--overwrite" in refused.stderr and output.read_bytes() == original

    replaced = invoke(
        "evaluation", "baseline", str(report_file), "--output", str(output), "--overwrite"
    )
    assert replaced.exit_code == 0 and output.read_bytes() == original

    same = invoke("evaluation", "baseline", str(report_file), "--output", str(report_file))
    assert same.exit_code == 2
    assert EvaluationReport.model_validate_json(report_file.read_text(encoding="utf-8"))

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    unopened = tmp_path / "must not exist" / "baseline.json"
    invalid = invoke("evaluation", "baseline", str(malformed), "--output", str(unopened))
    assert invalid.exit_code == 2 and invalid.stdout == ""
    assert not unopened.exists() and not unopened.parent.exists()


@pytest.mark.parametrize("kind", ["malformed", "missing-case", "tampered-metric", "mismatch"])
def test_invalid_inputs_exit_two_without_outputs(
    report_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    forbid_library(monkeypatch)
    baseline = tmp_path / "baseline.json"
    assert (
        invoke("evaluation", "baseline", str(report_file), "--output", str(baseline)).exit_code == 0
    )
    data = json.loads(report_file.read_text(encoding="utf-8"))
    if kind == "malformed":
        text = "{"
    elif kind == "missing-case":
        data["results"] = data["results"][:-1]
        text = json.dumps(data)
    elif kind == "tampered-metric":
        data["metrics"]["mean_recall"] = 0.25
        text = json.dumps(data)
    else:
        data["top_k"] = 4
        for case in data["results"]:
            case["retrieval"]["top_k"] = 4
        text = json.dumps(data)
    candidate = tmp_path / f"{kind}.json"
    candidate.write_text(text, encoding="utf-8")

    result = invoke("evaluation", "gate", str(baseline), str(candidate), "--json")
    assert result.exit_code == 2
    assert result.stdout == "" and "invalid" in result.stderr.lower()


def test_file_io_errors_are_one_and_do_not_create_database(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    missing = tmp_path / "missing report.json"
    output = tmp_path / "new parent" / "baseline.json"
    result = invoke("evaluation", "baseline", str(missing), "--output", str(output))
    assert result.exit_code == 1 and result.stdout == ""
    assert "could not read" in result.stderr
    assert not output.parent.exists()

    compare = invoke("evaluation", "compare", str(missing), str(missing), "--json")
    assert compare.exit_code == 1 and compare.stdout == ""

    def fail_write(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cli, "_atomic_write", fail_write)
    write_error = invoke(
        "evaluation",
        "baseline",
        str(report_file),
        "--output",
        str(tmp_path / "write-error.json"),
    )
    assert write_error.exit_code == 1 and write_error.stdout == ""
    assert "could not write baseline" in write_error.stderr


def test_no_answer_only_policy_has_no_applicable_checks_and_blocks(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    report = EvaluationReport.model_validate_json(report_file.read_text(encoding="utf-8"))
    item = report.dataset.items[1]
    dataset = report.dataset.model_copy(update={"items": (item,)})
    case = report.results[1]
    only = report.model_copy(
        update={
            "dataset": dataset,
            "dataset_hash": dataset.content_hash,
            "results": (case,),
            "metrics": aggregate_metrics((case.metrics,)),
        }
    )
    only = validate_evaluation_report(only)
    report_path = tmp_path / "no-answer-only.json"
    report_path.write_text(only.model_dump_json(indent=2), encoding="utf-8")
    baseline = tmp_path / "no-answer-baseline.json"
    assert (
        invoke("evaluation", "baseline", str(report_path), "--output", str(baseline)).exit_code == 0
    )

    policy = GatePolicy(rules=(GateRule(metric=MetricName.RECALL),))
    policy_path = tmp_path / "recall-policy.json"
    policy_path.write_text(dump_gate_policy(policy), encoding="utf-8")
    gated = invoke(
        "evaluation",
        "gate",
        str(baseline),
        str(report_path),
        "--policy",
        str(policy_path),
        "--json",
    )
    result = GateResult.model_validate_json(gated.stdout)
    assert gated.exit_code == 3 and result.passed is False
    assert result.checks and all(check.passed is None for check in result.checks)
    assert "no applicable numeric checks" in " ".join(result.violations)

    invalid_policy = tmp_path / "invalid-policy.json"
    invalid_policy.write_text("{", encoding="utf-8")
    invalid = invoke(
        "evaluation",
        "gate",
        str(baseline),
        str(report_path),
        "--policy",
        str(invalid_policy),
        "--json",
    )
    assert invalid.exit_code == 2 and invalid.stdout == ""
    assert "invalid gate policy" in invalid.stderr


def test_degraded_candidate_blocks_but_compare_succeeds_and_warns(
    report_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_library(monkeypatch)
    report = EvaluationReport.model_validate_json(report_file.read_text(encoding="utf-8"))
    missing_id = UUID(int=999)
    cases = tuple(
        case.model_copy(
            update={
                "retrieval": case.retrieval.model_copy(
                    update={"mode": RetrievalMode.VECTOR, "missing_vector_ids": (missing_id,)}
                )
            }
        )
        for case in report.results
    )
    degraded = validate_evaluation_report(
        report.model_copy(update={"mode": RetrievalMode.VECTOR, "results": cases})
    )
    degraded_path = tmp_path / "degraded.json"
    degraded_path.write_text(degraded.model_dump_json(indent=2), encoding="utf-8")
    refused_baseline = tmp_path / "refused-degraded-baseline.json"
    refused = invoke(
        "evaluation",
        "baseline",
        str(degraded_path),
        "--output",
        str(refused_baseline),
    )
    assert refused.exit_code == 2 and not refused_baseline.exists()
    assert "degraded" in refused.stderr

    healthy_vector = report.model_copy(
        update={
            "mode": RetrievalMode.VECTOR,
            "results": tuple(
                case.model_copy(
                    update={
                        "retrieval": case.retrieval.model_copy(
                            update={"mode": RetrievalMode.VECTOR}
                        )
                    }
                )
                for case in report.results
            ),
        }
    )
    healthy_path = tmp_path / "healthy-vector.json"
    healthy_path.write_text(
        validate_evaluation_report(healthy_vector).model_dump_json(), encoding="utf-8"
    )
    baseline = tmp_path / "vector-baseline.json"
    assert (
        invoke("evaluation", "baseline", str(healthy_path), "--output", str(baseline)).exit_code
        == 0
    )

    compared = invoke("evaluation", "compare", str(baseline), str(degraded_path), "--json")
    assert compared.exit_code == 0
    assert EvaluationComparison.model_validate_json(compared.stdout).degraded
    assert str(missing_id) in compared.stderr and "Warning:" in compared.stderr

    gated = invoke("evaluation", "gate", str(baseline), str(degraded_path), "--json")
    assert gated.exit_code == 3
    assert GateResult.model_validate_json(gated.stdout).passed is False
    assert str(missing_id) in gated.stderr


def test_main_and_python_module_process(report_file: Path, tmp_path: Path) -> None:
    baseline = tmp_path / "process baseline.json"
    assert cli.main(["evaluation", "baseline", str(report_file), "--output", str(baseline)]) == 0
    assert cli.main(["evaluation", "gate", str(baseline), str(report_file), "--json"]) == 0

    environment = os.environ.copy()
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "konwledge",
            "evaluation",
            "compare",
            str(baseline),
            str(report_file),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert process.returncode == 0, process.stderr
    assert EvaluationComparison.model_validate_json(process.stdout)
