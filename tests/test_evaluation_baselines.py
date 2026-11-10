"""真实语料报告与边界篡改测试；不修改 U15 模型和 JSON 契约。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from konwledge.domain import (
    Document,
    DocumentContentType,
    EvaluationItem,
    EvaluationSet,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.evaluation import (
    BaselineCase,
    EvaluationBaseline,
    EvaluationReport,
    EvaluationService,
    aggregate_metrics,
    baseline_from_report,
    dump_baseline,
    dump_evaluation_set,
    score_ranked_lineages,
    validate_evaluation_report,
)
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage import (
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)
SET_ID = UUID(int=101)
SOURCE_ID = UUID(int=102)
DOC_ID = UUID(int=103)
LINEAGE_ID = UUID(int=104)
OTHER_LINEAGE_ID = UUID(int=105)
MISSING_ID = UUID(int=999)
SECRET_QUOTE = "u16token 这是不能泄露的原文"
ABSOLUTE_PATH = "/tmp/u16-private-corpus/evidence.md"


@pytest.fixture
def report() -> EvaluationReport:
    """实际导入和检索：重复谱系占位、来源过滤、无答案和固定时间 case。"""
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    try:
        with UnitOfWork(factory) as uow:
            for source_id, doc_id in ((SOURCE_ID, DOC_ID), (UUID(int=202), UUID(int=203))):
                uow.sources.add(
                    Source(
                        id=source_id,
                        kind=SourceKind.FILE,
                        uri=f"{ABSOLUTE_PATH}.{source_id.int}",
                        created_at=NOW - timedelta(days=1),
                    )
                )
                uow.documents.add(
                    Document(
                        id=doc_id,
                        source_id=source_id,
                        locator=ABSOLUTE_PATH,
                        content_type=DocumentContentType.MARKDOWN,
                        checksum="a" * 64,
                        ingested_at=NOW - timedelta(days=1),
                    )
                )
            for ordinal, lineage_id in enumerate((LINEAGE_ID, LINEAGE_ID, OTHER_LINEAGE_ID)):
                uow.knowledge_units.add(
                    KnowledgeUnit(
                        id=UUID(int=301 + ordinal),
                        lineage_id=lineage_id,
                        document_id=DOC_ID,
                        ordinal=ordinal,
                        text=SECRET_QUOTE,
                        start_char=ordinal * 100,
                        end_char=ordinal * 100 + len(SECRET_QUOTE),
                    )
                )
            uow.knowledge_units.add(
                KnowledgeUnit(
                    id=UUID(int=401),
                    lineage_id=UUID(int=402),
                    document_id=UUID(int=203),
                    ordinal=0,
                    text=SECRET_QUOTE,
                    start_char=0,
                    end_char=len(SECRET_QUOTE),
                )
            )
            uow.commit()
        dataset = EvaluationSet(
            id=SET_ID,
            name="可移植评估",
            version=1,
            created_at=NOW - timedelta(hours=1),
            items=(
                EvaluationItem(
                    id="answer",
                    query="  u16token  ",
                    expected_lineage_ids=frozenset({LINEAGE_ID, OTHER_LINEAGE_ID}),
                    source_id=SOURCE_ID,
                ),
                EvaluationItem(id="none", query="zzneverfound", expect_no_answer=True),
                EvaluationItem(
                    id="anchored",
                    query="u16token",
                    expected_lineage_ids=frozenset({LINEAGE_ID}),
                    source_id=SOURCE_ID,
                    at=NOW - timedelta(minutes=30),
                    include_stale=True,
                ),
            ),
        )
        service = EvaluationService(factory, RetrievalService(factory), clock=lambda: NOW)
        service.import_set(dataset)
        result = service.run(SET_ID, top_k=3, candidate_limit=10, rrf_k=60)
        assert len(result.results[0].retrieval.hits) == 3
        assert result.results[1].retrieval.hits == ()
        return result
    finally:
        engine.dispose()


def change_retrieval(report: EvaluationReport, **changes: Any) -> EvaluationReport:
    case = report.results[0]
    changed = case.model_copy(update={"retrieval": case.retrieval.model_copy(update=changes)})
    return report.model_copy(update={"results": (changed, *report.results[1:])})


def change_hit(report: EvaluationReport, **changes: Any) -> EvaluationReport:
    retrieval = report.results[0].retrieval
    hit = retrieval.hits[0].model_copy(update=changes)
    return change_retrieval(report, hits=(hit, *retrieval.hits[1:]))


def assert_rejected(report: EvaluationReport) -> None:
    with pytest.raises(ValueError):
        validate_evaluation_report(report)
    with pytest.raises(ValueError):
        baseline_from_report(report, allow_degraded=True)


def test_real_report_roundtrip_portability_and_hash(report: EvaluationReport) -> None:
    original = report.model_dump_json()
    checked = validate_evaluation_report(report)
    assert checked == report
    assert checked is not report
    assert report.model_dump_json() == original
    assert ABSOLUTE_PATH in original
    assert SECRET_QUOTE in original
    assert report.dataset.items[0].query == "  u16token  "
    assert report.results[0].retrieval.query == "u16token"
    assert validate_evaluation_report(EvaluationReport.model_validate_json(original)) == report

    baseline = baseline_from_report(report)
    exported = dump_baseline(baseline)
    assert EvaluationBaseline.model_validate_json(exported) == baseline
    assert exported == dump_baseline(baseline_from_report(report))
    assert exported.endswith("\n") and not exported.endswith("\n\n")
    assert "可移植评估" in exported
    assert ABSOLUTE_PATH not in exported
    assert SECRET_QUOTE not in exported
    for key in ("source_uri", "locator", "document_id", "unit_id", "hits", "quote", "degraded"):
        assert f'"{key}"' not in exported
    assert str(DOC_ID) not in exported
    assert str(SOURCE_ID) in exported
    assert str(LINEAGE_ID) in exported
    assert baseline.dataset == report.dataset
    assert baseline.dataset.id == SET_ID and baseline.dataset.version == 1
    canonical = json.dumps(
        json.loads(dump_evaluation_set(report.dataset)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert baseline.dataset_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert baseline.dataset_hash == report.dataset.content_hash
    assert baseline.degraded is False
    assert baseline.cases[2].evaluated_at == report.dataset.items[2].at
    assert len(baseline.cases[0].ranked_lineage_ids) == 3
    assert len(set(baseline.cases[0].ranked_lineage_ids)) == 2
    assert baseline.cases[0].metrics.precision == 2 / 3
    with pytest.raises(ValidationError):
        baseline.top_k = 2
    with pytest.raises(ValidationError):
        baseline.cases[0].item_id = "changed"


@pytest.mark.parametrize("kind", ["missing", "duplicate", "swapped", "extra", "wrong_id"])
def test_report_case_completeness(report: EvaluationReport, kind: str) -> None:
    cases = report.results
    variants = {
        "missing": cases[:-1],
        "duplicate": (cases[0], cases[0], cases[2]),
        "swapped": (cases[1], cases[0], cases[2]),
        "extra": (*cases, cases[0]),
        "wrong_id": (cases[0].model_copy(update={"item_id": "unknown"}), *cases[1:]),
    }
    assert_rejected(report.model_copy(update={"results": variants[kind]}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query", "unrelated"),
        ("query", "  "),
        ("mode", RetrievalMode.VECTOR),
        ("top_k", 2),
        ("candidate_limit", 11),
        ("rrf_k", 61),
        ("include_stale", True),
        ("evaluated_at", NOW + timedelta(seconds=1)),
        ("evaluated_at", NOW.replace(tzinfo=None)),
        ("missing_vector_ids", (MISSING_ID,)),
        ("missing_vector_ids", (UUID(int=0),)),
        ("missing_vector_ids", (MISSING_ID, MISSING_ID)),
        ("top_k", True),
        ("include_stale", 0),
        ("extra_unknown", "secret"),
    ],
)
def test_report_retrieval_tampering(report: EvaluationReport, field: str, value: Any) -> None:
    assert_rejected(change_retrieval(report, **{field: value}))


@pytest.mark.parametrize("kind", ["graph_source", "graph_doc", "offset", "text", "nil"])
def test_report_evidence_tampering(report: EvaluationReport, kind: str) -> None:
    hit = report.results[0].retrieval.hits[0]
    if kind == "graph_source":
        bad = change_hit(report, document=hit.document.model_copy(update={"source_id": MISSING_ID}))
    elif kind == "graph_doc":
        bad = change_hit(report, unit=hit.unit.model_copy(update={"document_id": MISSING_ID}))
    elif kind == "offset":
        bad = change_hit(
            report, unit=hit.unit.model_copy(update={"end_char": hit.unit.end_char + 1})
        )
    elif kind == "text":
        bad = change_hit(report, unit=hit.unit.model_copy(update={"text": "   "}))
    else:
        bad = change_hit(report, unit=hit.unit.model_copy(update={"id": UUID(int=0)}))
    assert_rejected(bad)


def test_report_source_status_duplicates_and_limit(report: EvaluationReport) -> None:
    hit = report.results[0].retrieval.hits[0]
    assert_rejected(
        change_hit(
            report,
            source=hit.source.model_copy(update={"id": MISSING_ID}),
            document=hit.document.model_copy(update={"source_id": MISSING_ID}),
        )
    )
    for status in (
        KnowledgeUnitStatus.STALE,
        KnowledgeUnitStatus.SUPERSEDED,
        KnowledgeUnitStatus.DELETED,
    ):
        assert_rejected(change_hit(report, status=status))
    assert_rejected(change_retrieval(report, hits=(hit, hit)))
    assert_rejected(change_retrieval(report, hits=(*report.results[0].retrieval.hits, hit)))
    cases = list(report.results)
    anchored = cases[2]
    retrieval = anchored.retrieval
    stale_hit = retrieval.hits[0].model_copy(update={"status": KnowledgeUnitStatus.STALE})
    cases[2] = anchored.model_copy(
        update={
            "retrieval": retrieval.model_copy(update={"hits": (stale_hit, *retrieval.hits[1:])})
        }
    )
    assert validate_evaluation_report(report.model_copy(update={"results": tuple(cases)}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01, 0.25, True])
def test_report_case_metrics_are_recomputed(report: EvaluationReport, value: Any) -> None:
    case = report.results[0]
    changed = case.model_copy(update={"metrics": case.metrics.model_copy(update={"recall": value})})
    assert_rejected(report.model_copy(update={"results": (changed, *report.results[1:])}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_count", 4),
        ("case_count", True),
        ("answerable_count", 3),
        ("no_answer_count", 0),
        ("mean_recall", float("nan")),
        ("mean_ndcg", float("inf")),
        ("hit_rate", 0.25),
        ("no_answer_accuracy", None),
    ],
)
def test_report_aggregate_metrics_and_cohorts(
    report: EvaluationReport, field: str, value: Any
) -> None:
    assert_rejected(
        report.model_copy(update={"metrics": report.metrics.model_copy(update={field: value})})
    )


def test_report_identity_and_nested_dataset_spoof(report: EvaluationReport) -> None:
    assert_rejected(report.model_copy(update={"dataset_hash": "0" * 64}))
    assert_rejected(report.model_copy(update={"candidate_limit": 1}))
    item = report.dataset.items[0].model_copy(update={"query": " "})
    dataset = report.dataset.model_copy(update={"items": (item, *report.dataset.items[1:])})
    assert_rejected(
        report.model_copy(update={"dataset": dataset, "dataset_hash": dataset.content_hash})
    )
    case = report.results[1]
    changed = case.model_copy(
        update={"metrics": case.metrics.model_copy(update={"no_answer_success": 1})}
    )
    assert_rejected(
        report.model_copy(update={"results": (report.results[0], changed, report.results[2])})
    )
    assert_rejected(report.model_copy(update={"injected_path": ABSOLUTE_PATH}))
    with pytest.raises(ValueError):
        validate_evaluation_report(None)  # type: ignore[arg-type]


def degraded_report(report: EvaluationReport) -> EvaluationReport:
    cases = tuple(
        case.model_copy(
            update={
                "retrieval": case.retrieval.model_copy(
                    update={"mode": RetrievalMode.HYBRID, "missing_vector_ids": (MISSING_ID,)}
                )
            }
        )
        for case in report.results
    )
    return report.model_copy(update={"mode": RetrievalMode.HYBRID, "results": cases})


def test_degraded_candidate_snapshot(report: EvaluationReport) -> None:
    degraded = degraded_report(report)
    assert validate_evaluation_report(degraded).degraded
    with pytest.raises(ValueError, match="degraded"):
        baseline_from_report(degraded)
    compact = baseline_from_report(degraded, allow_degraded=True)
    assert compact.degraded
    assert compact.cases[0].missing_vector_ids == (MISSING_ID,)
    assert EvaluationBaseline.model_validate_json(dump_baseline(compact)).degraded
    with pytest.raises(ValueError):
        baseline_from_report(report, allow_degraded=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "swapped", "extra"])
def test_baseline_case_completeness(report: EvaluationReport, kind: str) -> None:
    baseline = baseline_from_report(report)
    cases = baseline.cases
    variants = {
        "missing": cases[:-1],
        "duplicate": (cases[0], cases[0], cases[2]),
        "swapped": (cases[1], cases[0], cases[2]),
        "extra": (*cases, cases[0]),
    }
    with pytest.raises(ValueError):
        dump_baseline(baseline.model_copy(update={"cases": variants[kind]}))


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, 2, None])
def test_baseline_schema_version_strict(report: EvaluationReport, value: Any) -> None:
    baseline = baseline_from_report(report)
    data = json.loads(dump_baseline(baseline))
    data["schema_version"] = value
    with pytest.raises(ValueError):
        EvaluationBaseline.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError):
        dump_baseline(baseline.model_copy(update={"schema_version": value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_k", True),
        ("top_k", "3"),
        ("top_k", 3.0),
        ("candidate_limit", 1),
        ("rrf_k", 0),
        ("dataset_hash", "0" * 64),
        ("source_uri", ABSOLUTE_PATH),
    ],
)
def test_baseline_fields_and_spoofed_instances(
    report: EvaluationReport, field: str, value: Any
) -> None:
    baseline = baseline_from_report(report)
    with pytest.raises(ValueError):
        dump_baseline(baseline.model_copy(update={field: value}))
    with pytest.raises(ValueError):
        EvaluationBaseline.model_validate(baseline.model_copy(update={field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_id", " "),
        ("ranked_lineage_ids", (UUID(int=0),)),
        ("missing_vector_ids", (UUID(int=0),)),
        ("missing_vector_ids", (MISSING_ID, MISSING_ID)),
        ("ranked_lineage_ids", [LINEAGE_ID]),
        ("evaluated_at", NOW.replace(tzinfo=None)),
        ("quote", SECRET_QUOTE),
    ],
)
def test_baseline_case_validation(report: EvaluationReport, field: str, value: Any) -> None:
    baseline = baseline_from_report(report)
    case = baseline.cases[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        BaselineCase.model_validate(case)
    with pytest.raises(ValueError):
        dump_baseline(baseline.model_copy(update={"cases": (case, *baseline.cases[1:])}))


def test_baseline_score_hash_cohort_rank_and_time_checks(report: EvaluationReport) -> None:
    baseline = baseline_from_report(report)
    case = baseline.cases[0]
    for changes in (
        {"ranked_lineage_ids": ()},
        {"ranked_lineage_ids": (*case.ranked_lineage_ids, LINEAGE_ID)},
        {"missing_vector_ids": (MISSING_ID,)},
        {"evaluated_at": NOW + timedelta(seconds=1)},
        {"metrics": case.metrics.model_copy(update={"recall": float("nan")})},
        {"metrics": case.metrics.model_copy(update={"no_answer_success": True})},
    ):
        copied = case.model_copy(update=changes)
        with pytest.raises(ValueError):
            dump_baseline(baseline.model_copy(update={"cases": (copied, *baseline.cases[1:])}))
    for aggregate_changes in (
        {"answerable_count": 3},
        {"mean_ndcg": float("inf")},
        {"case_count": 2},
    ):
        with pytest.raises(ValueError):
            dump_baseline(
                baseline.model_copy(
                    update={"metrics": baseline.metrics.model_copy(update=aggregate_changes)}
                )
            )
    with pytest.raises(ValueError):
        dump_baseline(None)  # type: ignore[arg-type]


def test_metric_epsilon_and_out_of_range(report: EvaluationReport) -> None:
    case = report.results[0]
    changed = case.model_copy(
        update={"metrics": case.metrics.model_copy(update={"precision": 2 / 3 + 5e-13})}
    )
    near = report.model_copy(update={"results": (changed, *report.results[1:])})
    assert baseline_from_report(near)
    changed = case.model_copy(
        update={"metrics": case.metrics.model_copy(update={"precision": 2 / 3 + 2e-12})}
    )
    assert_rejected(report.model_copy(update={"results": (changed, *report.results[1:])}))
    changed = case.model_copy(
        update={"metrics": case.metrics.model_copy(update={"recall": 1 + 5e-13})}
    )
    assert_rejected(report.model_copy(update={"results": (changed, *report.results[1:])}))


def test_dst_fold_and_equivalent_timezone(report: EvaluationReport) -> None:
    zone = ZoneInfo("America/New_York")
    anchor = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    other_fold = anchor.replace(fold=1)
    assert anchor == other_fold  # 展示直接比较的 DST 陷阱。
    dataset = report.dataset.model_copy(
        update={
            "items": (
                report.dataset.items[0].model_copy(update={"at": anchor}),
                *report.dataset.items[1:],
            )
        }
    )
    anchored = change_retrieval(report, evaluated_at=anchor.astimezone(UTC))
    anchored = anchored.model_copy(
        update={"dataset": dataset, "dataset_hash": dataset.content_hash}
    )
    baseline = baseline_from_report(anchored)
    assert baseline.cases[0].evaluated_at == anchor.astimezone(UTC)
    assert_rejected(change_retrieval(anchored, evaluated_at=other_fold))
    bad_case = baseline.cases[0].model_copy(update={"evaluated_at": other_fold})
    with pytest.raises(ValueError):
        dump_baseline(baseline.model_copy(update={"cases": (bad_case, *baseline.cases[1:])}))


def test_explicit_boundary_does_not_change_u15_model(report: EvaluationReport) -> None:
    data = report.model_dump(mode="python")
    data["results"] = ()
    # U15 构造行为不变；只在新边界拒绝不完整报告。
    legacy = EvaluationReport.model_validate(data)
    assert legacy.results == ()
    assert_rejected(legacy)


def test_validation_never_calls_services(
    report: EvaluationReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("validation invoked storage or retrieval")

    monkeypatch.setattr(UnitOfWork, "__enter__", forbidden)
    monkeypatch.setattr(EvaluationService, "run", forbidden)
    monkeypatch.setattr(RetrievalService, "search", forbidden)
    assert validate_evaluation_report(report)
    assert dump_baseline(baseline_from_report(report))


def test_rank_duplicates_keep_slots() -> None:
    metrics = score_ranked_lineages(
        (LINEAGE_ID, OTHER_LINEAGE_ID), (LINEAGE_ID, LINEAGE_ID, OTHER_LINEAGE_ID), k=3
    )
    assert metrics.precision == 2 / 3
    assert metrics.recall == 1
    assert aggregate_metrics((metrics,)).answerable_count == 1


@pytest.mark.parametrize("kind", ["score", "nan", "count", "no_answer", "nil", "missing", "time"])
def test_baseline_json_integrity(report: EvaluationReport, kind: str) -> None:
    data = json.loads(dump_baseline(baseline_from_report(report)))
    if kind == "score":
        data["cases"][0]["metrics"]["precision"] = 0.0
    elif kind == "nan":
        data["cases"][0]["metrics"]["ndcg"] = float("nan")
    elif kind == "count":
        data["metrics"]["no_answer_count"] = 2
    elif kind == "no_answer":
        data["cases"][1]["metrics"]["no_answer_success"] = False
    elif kind == "nil":
        data["cases"][0]["ranked_lineage_ids"][0] = str(UUID(int=0))
    elif kind == "missing":
        data["cases"][0]["missing_vector_ids"] = [str(MISSING_ID)]
    else:
        data["cases"][2]["evaluated_at"] = NOW.isoformat()
    with pytest.raises(ValueError):
        EvaluationBaseline.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01, True])
def test_standalone_case_rejects_bad_metrics(report: EvaluationReport, value: Any) -> None:
    case = baseline_from_report(report).cases[0]
    spoofed = case.model_copy(update={"metrics": case.metrics.model_copy(update={"recall": value})})
    with pytest.raises(ValueError):
        BaselineCase.model_validate(spoofed)


def test_hybrid_missing_ids_and_hit_scores(report: EvaluationReport) -> None:
    hybrid = degraded_report(report)
    for ids in ((UUID(int=0),), (MISSING_ID, MISSING_ID)):
        assert_rejected(change_retrieval(hybrid, missing_vector_ids=ids))
    for field in ("score", "lexical_score", "vector_score"):
        assert_rejected(change_hit(report, **{field: float("inf")}))
    assert_rejected(change_hit(report, lexical_rank=True))
    case = baseline_from_report(report).cases[0]
    equivalent = case.model_copy(update={"evaluated_at": NOW.astimezone(ZoneInfo("Asia/Shanghai"))})
    baseline = baseline_from_report(report)
    assert dump_baseline(baseline.model_copy(update={"cases": (equivalent, *baseline.cases[1:])}))


def test_gate_exports_are_exact() -> None:
    import konwledge.evaluation as evaluation
    from konwledge.evaluation import gates

    names = (
        "MetricName",
        "GateRule",
        "GatePolicy",
        "MetricDelta",
        "EvaluationComparison",
        "GateCheck",
        "GateResult",
        "compare_baseline",
        "evaluate_gate",
        "dump_gate_policy",
    )
    for name in names:
        assert name in evaluation.__all__
        assert getattr(evaluation, name) is getattr(gates, name)
    unknown_name = "unknown_evaluation_api"
    with pytest.raises(AttributeError):
        getattr(evaluation, unknown_name)
