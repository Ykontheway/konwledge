"""提交的 demo 检索基线：真实 SDK 报告、严格门禁与本地 CI 等价流程。

基线只能显式审阅更新；测试和 CI 绝不重新写入 examples 中的基线。
默认严格策略对六项指标同时检查汇总与逐 case，绝对降幅为零；
提交的 CI 策略额外要求有效评测时间一致，历史 case 保留自身 gold 时间。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jsonschema

from konwledge.application import KnowledgeLibrary
from konwledge.demo import build_demo
from konwledge.domain import EvaluationSet, KnowledgeUnitStatus
from konwledge.evaluation import (
    EvaluationBaseline,
    EvaluationReport,
    baseline_from_report,
    dump_baseline,
    validate_evaluation_report,
)
from konwledge.evaluation.gates import (
    GatePolicy,
    GateResult,
    MetricName,
    compare_baseline,
    dump_gate_policy,
    evaluate_gate,
)
from konwledge.retrieval import RetrievalMode
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "examples" / "evaluation-baseline.json"
POLICY = REPO / "examples" / "evaluation-gate.json"
DATASET = REPO / "examples" / "evaluation-demo.json"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
DATASET_ID = UUID("52522ebd-b1dc-4ace-824c-f8054c8547af")
DATASET_HASH = "8243bf211e3a089e6b7ea1d5235ba70515c3af96ce434cb5f40735df1ce8788c"
AT_TEXT = "2026-11-10T00:00:00Z"
AT = datetime(2026, 11, 10, tzinfo=UTC)
HISTORICAL_AT = datetime.fromisoformat("2026-10-10T12:00:00+08:00")
CI_STEPS = (
    "Demo snapshot is up to date",
    "Import demo evaluation dataset",
    "Evaluate demo retrieval at a fixed time",
    "Gate demo retrieval against the committed baseline",
)


@contextmanager
def _library(database: Path, *, at: datetime = AT) -> Iterator[KnowledgeLibrary]:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        create_schema(engine)
        yield KnowledgeLibrary(create_session_factory(engine), clock=lambda: at)
    finally:
        engine.dispose()


def _fresh_report(workspace: Path) -> EvaluationReport:
    demo = build_demo(workspace)
    dataset = EvaluationSet.model_validate_json(DATASET.read_bytes())
    with _library(demo.database) as library:
        imported = library.evaluation.import_set(dataset)
        assert imported.created
        report = library.evaluation.run(
            dataset.id,
            version=1,
            mode=RetrievalMode.LEXICAL,
            top_k=5,
            candidate_limit=50,
            rrf_k=60,
        )
    return validate_evaluation_report(report)


def _baseline() -> EvaluationBaseline:
    return EvaluationBaseline.model_validate_json(BASELINE.read_bytes())


def _policy() -> GatePolicy:
    return GatePolicy.model_validate_json(POLICY.read_bytes())


def _cli(*arguments: str, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "konwledge", *arguments],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    return result


def _workflow_script(name: str) -> tuple[str, ...]:
    """结构检查只读取当前 CI 的 run 块，不引入额外 YAML 测试依赖。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = f"      - name: {name}\n        run: |\n"
    assert text.count(marker) == 1
    commands: list[str] = []
    for line in text.split(marker, 1)[1].splitlines():
        if not line.startswith("          "):
            break
        commands.append(line.removeprefix("          "))
    assert commands
    return tuple(commands)


def _ci_command(
    command: str, runner_temp: Path, *, exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    """执行工作流原始参数，使用当前 uv Python 等价替代 uv run knowledge。

    重定向输出只保存至测试临时目录；不执行远程 action、不上传数据库。
    """
    tokens = [token.replace("$RUNNER_TEMP", str(runner_temp)) for token in shlex.split(command)]
    assert tokens[:3] == ["uv", "run", "knowledge"]
    arguments = tokens[3:]
    output: Path | None = None
    if ">" in arguments:
        index = arguments.index(">")
        assert len(arguments) == index + 2
        output = Path(arguments[index + 1])
        assert output.parent == runner_temp
        arguments = arguments[:index]
    result = _cli(*arguments, exit_code=exit_code)
    if output is not None:
        output.write_text(result.stdout, encoding="utf-8")
    return result


def test_baseline_is_byte_identical_across_distinct_demo_roots(tmp_path: Path) -> None:
    first_root = tmp_path / "first" / "demo"
    second_root = tmp_path / "elsewhere" / "nested" / "demo"
    first = baseline_from_report(_fresh_report(first_root))
    second = baseline_from_report(_fresh_report(second_root))
    committed = BASELINE.read_bytes()

    assert dump_baseline(first).encode("utf-8") == dump_baseline(second).encode("utf-8")
    assert dump_baseline(first).encode("utf-8") == committed
    assert dump_baseline(_baseline()).encode("utf-8") == committed
    assert _baseline() == first == second
    assert first.schema_version == 1
    assert first.dataset_hash == first.dataset.content_hash == DATASET_HASH
    assert not first.degraded
    assert all(not case.missing_vector_ids for case in first.cases)
    for root in (first_root, second_root):
        assert str(root.resolve()) not in committed.decode("utf-8")
    assert b'"retrieval"' not in committed
    assert b'"uri"' not in committed
    assert b'"text"' not in committed

    schema = EvaluationBaseline.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        json.loads(committed)
    )


def test_real_report_preserves_gold_snapshot_times_and_metrics(tmp_path: Path) -> None:
    report = _fresh_report(tmp_path / "demo")
    assert report.dataset == EvaluationSet.model_validate_json(DATASET.read_bytes())
    assert report.dataset.id == DATASET_ID
    assert report.dataset.version == 1
    assert report.dataset_hash == DATASET_HASH
    assert report.evaluated_at == AT
    assert report.mode is RetrievalMode.LEXICAL
    assert (report.top_k, report.candidate_limit, report.rrf_k) == (5, 50, 60)
    assert not report.degraded
    assert report.metrics.model_dump() == {
        "case_count": 6,
        "answerable_count": 4,
        "no_answer_count": 2,
        "mean_recall": 1.0,
        "mean_precision": 0.25,
        "mean_reciprocal_rank": 1.0,
        "mean_ndcg": 1.0,
        "hit_rate": 1.0,
        "no_answer_accuracy": 1.0,
    }
    for item, case in zip(report.dataset.items, report.results, strict=True):
        assert case.item_id == item.id
        assert case.retrieval.evaluated_at == (item.at or AT)
        if item.id == "guide-port-historical":
            assert item.at == HISTORICAL_AT
            assert case.retrieval.hits[0].unit.heading == "服务端口"
            assert "8000" in case.retrieval.hits[0].unit.text
            assert "环境变量 PORT" not in case.retrieval.hits[0].unit.text
        if item.id == "guide-port-current":
            assert "8080" in case.retrieval.hits[0].unit.text
            assert "环境变量 PORT" in case.retrieval.hits[0].unit.text


def test_committed_policy_is_default_strict_with_same_time_and_passes(tmp_path: Path) -> None:
    policy = _policy()
    assert policy.schema_version == 1
    assert policy.require_same_time is True
    assert policy.rules == GatePolicy.default().rules
    assert tuple(rule.metric for rule in policy.rules) == tuple(MetricName)
    for rule in policy.rules:
        assert rule.scope == "both"
        assert rule.max_abs_drop == 0.0
        assert rule.min_value is None
        assert rule.max_relative_drop is None
    assert dump_gate_policy(policy).encode("utf-8") == POLICY.read_bytes()
    assert (
        dump_gate_policy(GatePolicy(require_same_time=True)).encode("utf-8") == POLICY.read_bytes()
    )
    schema = GatePolicy.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(json.loads(POLICY.read_bytes()))

    comparison = compare_baseline(_baseline(), _fresh_report(tmp_path / "demo"))
    result = evaluate_gate(comparison, policy)
    assert result.passed
    assert result.violations == ()
    assert len(result.checks) == 6 * 7
    assert all(check.passed is not False for check in result.checks)
    assert {check.metric for check in result.checks if check.item_id is None} == set(MetricName)
    assert all(delta.delta in (None, 0.0) for delta in comparison.deltas)


def test_workflow_keeps_strict_gate_and_report_only_always_artifacts() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "on:\n  push:\n    branches: [main]\n  pull_request:\n" in text
    assert 'python-version: ["3.11", "3.12", "3.13"]' in text
    positions = [text.index(f"      - name: {name}") for name in CI_STEPS]
    assert positions == sorted(positions)
    assert positions[-1] < text.index("  web:")
    quality = text.split("  web:", 1)[0]
    assert "continue-on-error" not in quality
    assert "||" not in quality
    assert "knowledge evaluation baseline" not in quality
    assert "--overwrite" not in quality

    commands = [command for name in CI_STEPS for command in _workflow_script(name)]
    assert len(commands) == 5
    assert shlex.split(commands[2]) == [
        "uv",
        "run",
        "knowledge",
        "evaluation",
        "import",
        str(DATASET.relative_to(REPO)),
        "-d",
        "$RUNNER_TEMP/demo/knowledge.db",
    ]
    assert shlex.split(commands[3]) == [
        "uv",
        "run",
        "knowledge",
        "evaluation",
        "run",
        str(DATASET_ID),
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
        AT_TEXT,
        "-d",
        "$RUNNER_TEMP/demo/knowledge.db",
        "--json",
        ">",
        "$RUNNER_TEMP/evaluation-candidate.json",
    ]
    assert shlex.split(commands[4]) == [
        "uv",
        "run",
        "knowledge",
        "evaluation",
        "gate",
        str(BASELINE.relative_to(REPO)),
        "$RUNNER_TEMP/evaluation-candidate.json",
        "--policy",
        str(POLICY.relative_to(REPO)),
        "--json",
        ">",
        "$RUNNER_TEMP/evaluation-gate.json",
    ]
    upload = quality.split("      - name: Upload evaluation evidence\n", 1)[1]
    assert "        if: always()\n" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    assert "name: evaluation-${{ matrix.python-version }}" in upload
    assert "if-no-files-found: ignore" in upload
    assert "          path: |\n" in upload
    assert "${{ runner.temp }}/evaluation-candidate.json" in upload
    assert "${{ runner.temp }}/evaluation-gate.json" in upload
    assert ".db" not in upload
    assert "demo/" not in upload


def test_local_workflow_cli_passes_then_real_stale_mark_blocks(tmp_path: Path) -> None:
    before = BASELINE.read_bytes()
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    for name in CI_STEPS:
        for command in _workflow_script(name):
            _ci_command(command, runner_temp)

    candidate_path = runner_temp / "evaluation-candidate.json"
    gate_path = runner_temp / "evaluation-gate.json"
    healthy = validate_evaluation_report(
        EvaluationReport.model_validate_json(candidate_path.read_bytes())
    )
    assert dump_baseline(baseline_from_report(healthy)).encode("utf-8") == before
    passed = GateResult.model_validate_json(gate_path.read_bytes())
    assert passed.passed
    assert passed.violations == ()
    assert passed.policy == _policy()

    current = next(case for case in healthy.results if case.item_id == "guide-port-current")
    [hit] = current.retrieval.hits
    with _library(runner_temp / "demo" / "knowledge.db", at=AT - timedelta(seconds=1)) as library:
        library.lifecycle.mark_stale(hit.unit.id, reason="本地门禁回归测试：当前端口证据过期")
        state = next(
            state for state in library.snapshot(AT).effective_units if state.unit.id == hit.unit.id
        )
        assert state.status is KnowledgeUnitStatus.STALE

    [run_command] = _workflow_script(CI_STEPS[2])
    _ci_command(run_command, runner_temp)
    candidate = validate_evaluation_report(
        EvaluationReport.model_validate_json(candidate_path.read_bytes())
    )
    failed_case = next(case for case in candidate.results if case.item_id == "guide-port-current")
    assert failed_case.retrieval.hits == ()
    assert failed_case.metrics.recall == failed_case.metrics.hit_rate == 0.0
    assert candidate.metrics.mean_recall == 0.75
    assert not candidate.degraded  # 真实词法质量回归，不是假造报告或缺失向量。
    historical = next(case for case in candidate.results if case.item_id == "guide-port-historical")
    assert historical.metrics.recall == 1.0
    assert historical.retrieval.evaluated_at == HISTORICAL_AT

    [gate_command] = _workflow_script(CI_STEPS[3])
    blocked_process = _ci_command(gate_command, runner_temp, exit_code=3)
    blocked = GateResult.model_validate_json(gate_path.read_bytes())
    assert not blocked.passed
    assert blocked.violations
    assert any("case guide-port-current recall" in violation for violation in blocked.violations)
    assert "Violation:" in blocked_process.stderr
    sdk_result = evaluate_gate(compare_baseline(_baseline(), candidate), _policy())
    assert blocked == sdk_result
    assert BASELINE.read_bytes() == before


def test_baseline_cli_requires_overwrite_and_never_changes_committed_file(tmp_path: Path) -> None:
    committed = BASELINE.read_bytes()
    report = _fresh_report(tmp_path / "demo")
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "baseline.json"
    created = _cli("evaluation", "baseline", str(report_path), "-o", str(output), "--json")
    assert created.stdout.encode("utf-8") == output.read_bytes() == committed
    assert output.stat().st_mode & 0o777 == 0o644

    refused = _cli("evaluation", "baseline", str(report_path), "-o", str(output), exit_code=2)
    assert "--overwrite" in refused.stderr
    assert output.read_bytes() == committed
    _cli("evaluation", "baseline", str(report_path), "-o", str(output), "--overwrite")
    assert output.read_bytes() == committed
    assert BASELINE.read_bytes() == committed
