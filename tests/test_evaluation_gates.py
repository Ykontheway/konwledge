from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from konwledge.domain import (
    Document,
    DocumentContentType,
    EvaluationItem,
    EvaluationSet,
    KnowledgeUnit,
    Source,
    SourceKind,
)
from konwledge.domain.models import KnowledgeUnitStatus
from konwledge.evaluation import (
    EvaluationReport,
    aggregate_metrics,
    baseline_from_report,
    score_ranked_lineages,
    validate_evaluation_report,
)
from konwledge.evaluation.gates import (
    GatePolicy,
    GateResult,
    GateRule,
    MetricName,
    compare_baseline,
    dump_gate_policy,
    evaluate_gate,
)
from konwledge.evaluation.models import EvaluationCaseResult
from konwledge.retrieval import RetrievalMode
from konwledge.retrieval.models import RetrievalHit, RetrievalResult

NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)
SET_ID = UUID(int=101)
SOURCE_ID = UUID(int=102)
DOC_ID = UUID(int=103)
LINEAGE_A = UUID(int=201)
LINEAGE_B = UUID(int=202)
MISSING_ID = UUID(int=999)


def create_report(
    items: list[EvaluationItem],
    hits_map: dict[str, list[UUID]],
    missing_vectors_map: dict[str, list[UUID]] | None = None,
    mode: RetrievalMode = RetrievalMode.VECTOR,
) -> EvaluationReport:
    missing_vectors_map = missing_vectors_map or {}

    source = Source(
        id=SOURCE_ID,
        kind=SourceKind.FILE,
        uri="/tmp/test.md",
        created_at=NOW - timedelta(days=1),
    )
    doc = Document(
        id=DOC_ID,
        source_id=SOURCE_ID,
        locator="/tmp/test.md",
        content_type=DocumentContentType.MARKDOWN,
        checksum="a" * 64,
        ingested_at=NOW - timedelta(days=1),
    )

    dataset = EvaluationSet(
        id=SET_ID,
        name="Gate Test",
        version=1,
        created_at=NOW - timedelta(hours=1),
        items=tuple(items),
    )

    results = []
    scores = []
    for item in items:
        returned = hits_map.get(item.id, [])
        missing = tuple(missing_vectors_map.get(item.id, []))

        hits = []
        for idx, lin in enumerate(returned):
            unit = KnowledgeUnit(
                id=UUID(int=2000 + idx + hash(item.id) % 1000),
                lineage_id=lin,
                document_id=DOC_ID,
                ordinal=idx,
                text=f"text for {lin}",
                start_char=idx * 100,
                end_char=idx * 100 + len(f"text for {lin}"),
            )
            hits.append(
                RetrievalHit(
                    unit=unit,
                    document=doc,
                    source=source,
                    status=KnowledgeUnitStatus.ACTIVE,
                    score=1.0,
                )
            )

        res = RetrievalResult(
            query=item.query,
            mode=mode,
            evaluated_at=item.at or NOW,
            include_stale=item.include_stale,
            top_k=3,
            candidate_limit=10,
            rrf_k=60,
            hits=tuple(hits),
            missing_vector_ids=missing,
        )

        item_score = score_ranked_lineages(
            item.expected_lineage_ids,
            tuple(lin for lin in returned),
            k=3,
            expect_no_answer=item.expect_no_answer,
        )

        results.append(EvaluationCaseResult(item_id=item.id, retrieval=res, metrics=item_score))
        scores.append(item_score)

    report = EvaluationReport(
        dataset=dataset,
        dataset_hash=dataset.content_hash,
        evaluated_at=NOW,
        mode=mode,
        top_k=3,
        candidate_limit=10,
        rrf_k=60,
        results=tuple(results),
        metrics=aggregate_metrics(scores),
    )
    return validate_evaluation_report(report)


def test_gate_policy_validation() -> None:
    # Reject empty rules
    with pytest.raises(ValueError, match="must not be empty"):
        GatePolicy(rules=())

    # Reject duplicate metrics
    with pytest.raises(ValueError, match="duplicate metric rules"):
        GatePolicy(rules=(GateRule(metric=MetricName.RECALL), GateRule(metric=MetricName.RECALL)))

    # Reject rule without bounds
    with pytest.raises(ValueError, match="must specify at least one bound"):
        GateRule(
            metric=MetricName.RECALL,
            min_value=None,
            max_abs_drop=None,
            max_relative_drop=None,
        )

    # Default policy
    default = GatePolicy.default()
    assert len(default.rules) == 6
    assert all(r.max_abs_drop == 0.0 for r in default.rules)
    assert all(r.scope == "both" for r in default.rules)


def test_comparison_and_gate_pass() -> None:
    items = [
        EvaluationItem(id="q1", query="query 1", expected_lineage_ids=frozenset({LINEAGE_A})),
        EvaluationItem(id="q2", query="query 2", expect_no_answer=True),
    ]

    # Baseline: both perfect
    baseline_report = create_report(items, {"q1": [LINEAGE_A], "q2": []})
    baseline = baseline_from_report(baseline_report)

    # Candidate: same
    candidate_report = create_report(items, {"q1": [LINEAGE_A], "q2": []})

    comparison = compare_baseline(baseline, candidate_report)
    assert comparison.candidate.metrics == baseline.metrics

    result = evaluate_gate(comparison)
    assert result.passed is True
    assert not result.violations


def test_gate_regression_fail() -> None:
    items = [EvaluationItem(id="q1", query="query 1", expected_lineage_ids=frozenset({LINEAGE_A}))]

    # Baseline: recall 1.0
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))

    # Candidate: recall 0.0
    candidate_report = create_report(items, {"q1": [LINEAGE_B]})

    comparison = compare_baseline(baseline, candidate_report)

    # Default policy (max_abs_drop=0) should fail
    result = evaluate_gate(comparison)
    assert result.passed is False
    assert any("recall" in v and "absolute drop" in v for v in result.violations)

    # Policy allowing drop should pass
    passing_policy = GatePolicy(
        rules=(
            GateRule(metric=MetricName.RECALL, max_abs_drop=1.0),
            GateRule(metric=MetricName.PRECISION, max_abs_drop=1.0),
            GateRule(metric=MetricName.MRR, max_abs_drop=1.0),
            GateRule(metric=MetricName.NDCG, max_abs_drop=1.0),
            GateRule(metric=MetricName.HIT_RATE, max_abs_drop=1.0),
            GateRule(metric=MetricName.NO_ANSWER_ACCURACY, max_abs_drop=1.0),
        )
    )
    result2 = evaluate_gate(comparison, passing_policy)
    assert result2.passed is True


def test_gate_min_value_fail() -> None:
    items = [EvaluationItem(id="q1", query="query 1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))
    candidate_report = create_report(items, {"q1": [LINEAGE_A]})

    comparison = compare_baseline(baseline, candidate_report)

    # Policy with high min_value
    policy = GatePolicy(
        rules=(GateRule(metric=MetricName.RECALL, min_value=0.9, max_abs_drop=1.0),)
    )

    # Baseline/Candidate is 1.0, should pass
    assert evaluate_gate(comparison, policy).passed is True

    # Candidate with 0.0
    bad_candidate = create_report(items, {"q1": [LINEAGE_B]})
    comp2 = compare_baseline(baseline, bad_candidate)
    assert evaluate_gate(comp2, policy).passed is False


def test_gate_relative_drop() -> None:
    items = [
        EvaluationItem(
            id="q1",
            query="query 1",
            expected_lineage_ids=frozenset({LINEAGE_A, LINEAGE_B}),
        )
    ]
    # Baseline: 2/3 precision (top_k=3)
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A, LINEAGE_B]}))

    # Candidate: 1/3 precision
    candidate = create_report(items, {"q1": [LINEAGE_A]})

    comp = compare_baseline(baseline, candidate)

    # Max rel drop 0.4 should fail (rel drop is 0.5)
    policy1 = GatePolicy(
        rules=(GateRule(metric=MetricName.PRECISION, max_relative_drop=0.4, max_abs_drop=None),)
    )
    assert evaluate_gate(comp, policy1).passed is False

    # Max rel drop 0.6 should pass
    policy2 = GatePolicy(
        rules=(GateRule(metric=MetricName.PRECISION, max_relative_drop=0.6, max_abs_drop=None),)
    )
    assert evaluate_gate(comp, policy2).passed is True


def test_gate_fail_closed_missing_vectors() -> None:
    items = [EvaluationItem(id="q1", query="query 1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))

    # Candidate with missing vectors
    candidate_report = create_report(
        items, {"q1": [LINEAGE_A]}, missing_vectors_map={"q1": [MISSING_ID]}
    )

    comp = compare_baseline(baseline, candidate_report)
    result = evaluate_gate(comp)
    assert result.passed is False
    assert "missing vectors" in result.violations[0]


def test_gate_require_same_time() -> None:
    items = [EvaluationItem(id="q1", query="query 1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))

    # Candidate report may be evaluated at a different common time without changing gold.
    candidate_report = create_report(items, {"q1": [LINEAGE_A]}).model_copy(
        update={"evaluated_at": NOW + timedelta(seconds=1)}
    )
    candidate_case = candidate_report.results[0]
    candidate_report = candidate_report.model_copy(
        update={
            "results": (
                candidate_case.model_copy(
                    update={
                        "retrieval": candidate_case.retrieval.model_copy(
                            update={"evaluated_at": NOW + timedelta(seconds=1)}
                        )
                    }
                ),
            )
        }
    )

    comp = compare_baseline(baseline, candidate_report)

    # Default policy (require_same_time=False) should pass
    assert evaluate_gate(comp).passed is True

    # Policy with require_same_time=True should fail
    policy = GatePolicy(rules=GatePolicy.default().rules, require_same_time=True)
    result = evaluate_gate(comp, policy)
    assert result.passed is False
    assert "evaluation time differs" in result.violations[0]


def test_compensation_fail() -> None:
    items = [
        EvaluationItem(id="q1", query="q1", expected_lineage_ids=frozenset({LINEAGE_A})),
        EvaluationItem(id="q2", query="q2", expected_lineage_ids=frozenset({LINEAGE_B})),
    ]

    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A], "q2": []}))
    candidate = create_report(items, {"q1": [], "q2": [LINEAGE_B]})

    comp = compare_baseline(baseline, candidate)

    # Aggregate recall is the same (0.5)
    # Default policy has scope="both", so case q1 regression should catch it.
    result = evaluate_gate(comp)
    assert result.passed is False
    assert any("case q1" in v and "recall" in v for v in result.violations)


def test_mismatch_refusal() -> None:
    items = [EvaluationItem(id="q1", query="q1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))

    # Dataset mismatch
    items2 = [EvaluationItem(id="q2", query="q2", expected_lineage_ids=frozenset({LINEAGE_A}))]
    report2 = create_report(items2, {"q2": [LINEAGE_A]})
    with pytest.raises(ValueError, match="dataset snapshot does not match"):
        compare_baseline(baseline, report2)

    # Config mismatch
    report3 = create_report(items, {"q1": [LINEAGE_A]}, mode=RetrievalMode.LEXICAL)
    with pytest.raises(ValueError, match="mode does not match"):
        compare_baseline(baseline, report3)


def test_json_roundtrip_and_tamper() -> None:
    items = [EvaluationItem(id="q1", query="q1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))
    candidate = create_report(items, {"q1": [LINEAGE_A]})
    comp = compare_baseline(baseline, candidate)

    # Policy roundtrip
    policy = GatePolicy.default()
    assert GatePolicy.model_validate_json(dump_gate_policy(policy)) == policy

    # Result roundtrip
    result = evaluate_gate(comp, policy)
    dumped = result.model_dump_json()
    assert GateResult.model_validate_json(dumped) == result

    # Tamper deltas
    data = json.loads(dumped)
    data["comparison"]["deltas"][0]["delta"] = 100.0
    with pytest.raises(ValueError, match="delta does not match"):
        GateResult.model_validate_json(json.dumps(data))


def test_no_applicable_checks() -> None:
    items = [EvaluationItem(id="q1", query="q1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    baseline = baseline_from_report(create_report(items, {"q1": [LINEAGE_A]}))
    candidate = create_report(items, {"q1": [LINEAGE_A]})
    comp = compare_baseline(baseline, candidate)

    # Policy with irrelevant metric for this cohort
    # q1 is answerable, so NO_ANSWER_ACCURACY is None.
    policy = GatePolicy(rules=(GateRule(metric=MetricName.NO_ANSWER_ACCURACY, scope="both"),))

    result = evaluate_gate(comp, policy)
    assert result.passed is False
    assert "no applicable numeric checks" in result.violations[0]


def test_epsilon_boundary() -> None:
    items = [EvaluationItem(id="q1", query="q1", expected_lineage_ids=frozenset({LINEAGE_A}))]
    report = create_report(items, {"q1": [LINEAGE_A]})
    baseline = baseline_from_report(report)

    # Create candidate with slightly different metric (within epsilon)
    data = report.model_dump(mode="python")
    # precision is 1/3 ~ 0.3333333333333333
    # 1e-12 epsilon. Try a drop of 0.8e-12.
    data["results"][0]["metrics"]["precision"] -= 0.8e-12
    data["metrics"]["mean_precision"] -= 0.8e-12

    candidate = EvaluationReport.model_validate(data)
    comp = compare_baseline(baseline, candidate)

    # Default gate has max_abs_drop=0.0.
    # The drop is 0.8e-12, which is <= 1e-12 epsilon, so it should pass.
    assert evaluate_gate(comp).passed is True

    # A drop of 1.2e-12 will be rejected by report validation itself
    data["results"][0]["metrics"]["precision"] = 1 / 3 - 1.2e-12
    data["metrics"]["mean_precision"] = 1 / 3 - 1.2e-12
    candidate2 = EvaluationReport.model_validate(data)
    with pytest.raises(ValueError, match=r"metrics\.precision does not match"):
        compare_baseline(baseline, candidate2)
