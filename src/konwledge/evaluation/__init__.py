"""Retrieval evaluation, portable baselines, and deterministic regression gates."""

from konwledge.domain.evaluation import EvaluationItem, EvaluationSet, dump_evaluation_set
from konwledge.evaluation.baselines import (
    BaselineCase,
    EvaluationBaseline,
    baseline_from_report,
    dump_baseline,
)
from konwledge.evaluation.gates import (
    EvaluationComparison,
    GateCheck,
    GatePolicy,
    GateResult,
    GateRule,
    MetricDelta,
    MetricName,
    compare_baseline,
    dump_gate_policy,
    evaluate_gate,
)
from konwledge.evaluation.metrics import aggregate_metrics, score_ranked_lineages
from konwledge.evaluation.models import (
    AggregateMetrics,
    EvaluationCaseResult,
    EvaluationReport,
    ItemMetrics,
)
from konwledge.evaluation.service import (
    EvaluationImportResult,
    EvaluationService,
    EvaluationSetNotFoundError,
)
from konwledge.evaluation.validation import validate_evaluation_report

__all__ = [
    "AggregateMetrics",
    "BaselineCase",
    "EvaluationBaseline",
    "EvaluationCaseResult",
    "EvaluationComparison",
    "EvaluationImportResult",
    "EvaluationItem",
    "EvaluationReport",
    "EvaluationService",
    "EvaluationSet",
    "EvaluationSetNotFoundError",
    "GateCheck",
    "GatePolicy",
    "GateResult",
    "GateRule",
    "ItemMetrics",
    "MetricDelta",
    "MetricName",
    "aggregate_metrics",
    "baseline_from_report",
    "compare_baseline",
    "dump_baseline",
    "dump_evaluation_set",
    "dump_gate_policy",
    "evaluate_gate",
    "score_ranked_lineages",
    "validate_evaluation_report",
]
