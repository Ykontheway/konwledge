"""真实文档链路, 不使用 Demo/公开 benchmark 或伪业务 fixture。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from scripts.real_knowledge_acceptance import (
    CASES,
    ROOT,
    SOURCE_PATHS,
    Runner,
    build_dataset,
    read_library,
    run_acceptance,
)

from konwledge.domain import EvaluationSet
from konwledge.quality import QualityPolicy


def test_real_documents_cli_change_failure_restore(tmp_path: Path) -> None:
    sources_before = {relative: (ROOT / relative).read_bytes() for relative in SOURCE_PATHS}
    # 与用户实际调用相同的进程边界, 测试临时目录也是全新文件型 SQLite。
    result = subprocess.run(
        [sys.executable, "scripts/real_knowledge_acceptance.py", "--output-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    summary = json.loads(result.stdout)
    report_path = Path(summary["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.returncode == 0, report
    assert report["status"] == "passed"
    assert all(report["checks"].values())
    assert report["initial"]["report"]["passed"] is True
    assert report["initial"]["job_id"] is None
    assert report["config"]["quality_policy"] == QualityPolicy().model_dump(mode="json")
    assert report["baseline"]["human_accepted"] is False
    assert report["baseline"]["reason_code"] == "AWAITING_HUMAN_APPROVAL"
    assert report["baseline"]["regression_enabled"] is False
    steps = report["steps"]
    assert [step["job"]["status"] for step in steps] == ["passed", "failed", "passed"]
    assert [step["import_exit_code"] for step in steps] == [0, 3, 0]
    assert len({step["job"]["id"] for step in steps}) == 3
    assert {step["job"]["dataset"]["hash"] for step in steps} == {report["dataset"]["hash"]}
    fault = steps[1]
    assert fault["removed_evidence_hits"] == 0
    assert fault["include_stale_removed_evidence_hits"] == 0
    assert fault["job"]["error_code"] == "QUALITY_GATE_FAILED"
    assert fault["job"]["report"]["metrics"]["mean_recall"] < QualityPolicy().min_recall
    assert report["repeat_initial"]["before_job_count"] == 0
    assert report["repeat_initial"]["after_job_count"] == 0
    assert report["repeat_restored"]["before_job_count"] == 3
    assert report["repeat_restored"]["after_job_count"] == 3
    history = json.loads((report_path.parent / "history.json").read_text())
    assert fault["job"] in history
    for step in steps:
        stored = json.loads((report_path.parent / f"{step['name']}-job.json").read_text())
        assert stored == step["job"]
        assert stored["trigger_revision_ids"]
        assert stored["report"]["metrics"]["case_count"] == 4
    assert not any(
        command["command"] in ("knowledge auto-evaluation run", "knowledge auto-evaluation retry")
        for command in report["commands"]
    )
    dataset = EvaluationSet.model_validate_json(
        (report_path.parent / "evaluation.json").read_text()
    )
    with read_library(report_path.parent / "knowledge.db") as library:
        assert library.evaluation.get(dataset.id, version=1) == dataset
        target = next(iter(dataset.items[0].expected_lineage_ids))
        versions = library.unit_history(target)
        assert len(versions.versions) == 3
        state = library.unit_state(target)
        assert state is not None and state.status.value == "active"
    public_json = [
        report_path,
        report_path.parent / "history.json",
        report_path.parent / "initial-quality.json",
        report_path.parent / "config.json",
        report_path.parent / "case-manifest.json",
        report_path.parent / "lineages.json",
    ]
    public_json += list(report_path.parent.glob("*-job.json"))
    for path in public_json:
        rendered = path.read_text(encoding="utf-8")
        assert '"query"' not in rendered
        assert '"text"' not in rendered
        assert str(ROOT) not in rendered
        assert str(tmp_path) not in rendered
        assert "sqlite+pysqlite:" not in rendered
        for case in CASES:
            assert case.query not in rendered
        for raw in sources_before.values():
            assert raw.decode() not in rendered
    assert {relative: (ROOT / relative).read_bytes() for relative in SOURCE_PATHS} == sources_before


def test_case_expectations_are_fixed_before_retrieval() -> None:
    references = {ref for case in CASES for ref in case.references}
    mapping = {ref: uuid4() for ref in references}
    first = build_dataset(mapping)
    second = build_dataset(mapping)
    assert first.items == second.items
    assert len(first.items) == 4
    assert sum(item.expect_no_answer for item in first.items) == 1
    assert sum(len(item.expected_lineage_ids) > 1 for item in first.items) == 1
    assert all(item.at is None and not item.include_stale for item in first.items)
    for case, item in zip(CASES, first.items, strict=True):
        assert item.query == case.query
        assert item.expected_lineage_ids == frozenset(mapping[ref] for ref in case.references)
    attribute = "query"
    with pytest.raises(FrozenInstanceError):
        setattr(CASES[0], attribute, "changed")
    with pytest.raises(ValidationError):
        setattr(first.items[0], attribute, "changed")


def test_runner_error_preserves_unique_safe_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_self: Runner) -> None:
        raise RuntimeError("private query and sqlite+pysqlite:///secret/password.db")

    monkeypatch.setattr(Runner, "execute", fail)
    first = run_acceptance(tmp_path)
    original = first.read_bytes()
    second = run_acceptance(tmp_path)
    assert first != second
    assert first.read_bytes() == original
    assert json.loads(original)["error_code"] == "RUNTIMEERROR"
    assert "secret" not in original.decode()
    assert json.loads(second.read_text())["status"] == "failed"
