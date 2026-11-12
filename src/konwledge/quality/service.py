"""Independent quality acceptance built on one real evaluation run."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from konwledge.context.citations import citation_from_hit, verify_citation
from konwledge.domain import KnowledgeUnitStatus
from konwledge.evaluation import (
    EvaluationBaseline,
    EvaluationService,
    GatePolicy,
    baseline_from_report,
    compare_baseline,
    evaluate_gate,
)
from konwledge.quality.models import QualityCheck, QualityPolicy, QualityReport
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage import SessionFactory

Clock = Callable[[], datetime]


class QualityService:
    """Apply absolute quality and optional regression gates to real retrieval evidence."""

    def __init__(
        self,
        session_factory: SessionFactory,
        retrieval_service: RetrievalService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._evaluation = EvaluationService(
            session_factory,
            retrieval_service,
            clock=clock,
        )

    def run(
        self,
        set_id: UUID,
        *,
        version: int | None = None,
        policy: QualityPolicy | None = None,
        baseline: EvaluationBaseline | None = None,
        regression_policy: GatePolicy | None = None,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
    ) -> QualityReport:
        """Run every case once, then derive all checks from that report and its hits."""

        selected = (
            QualityPolicy()
            if policy is None
            else QualityPolicy.model_validate(policy.model_dump(), strict=True)
        )
        evaluation = self._evaluation.run(
            set_id,
            version=version,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            at=at,
        )
        snapshot = baseline_from_report(evaluation, allow_degraded=True)
        checks: list[QualityCheck] = [
            QualityCheck(
                name="top_k_contract",
                passed=top_k == selected.required_top_k,
                measured=top_k,
                required=f"=={selected.required_top_k}",
                detail=(
                    None
                    if top_k == selected.required_top_k
                    else f"policy requires top_k={selected.required_top_k} for stable metrics"
                ),
            )
        ]

        recall = evaluation.metrics.mean_recall
        checks.append(
            QualityCheck(
                name="recall_at_k",
                passed=recall is not None and recall >= selected.min_recall,
                measured=recall,
                required=f">={selected.min_recall}",
                detail=None if recall is not None else "undefined: dataset has no answerable cases",
            )
        )
        mrr = evaluation.metrics.mean_reciprocal_rank
        checks.append(
            QualityCheck(
                name="mean_reciprocal_rank",
                passed=mrr is not None and mrr >= selected.min_mrr,
                measured=mrr,
                required=f">={selected.min_mrr}",
                detail=None if mrr is not None else "undefined: dataset has no answerable cases",
            )
        )
        no_answer_accuracy = evaluation.metrics.no_answer_accuracy
        checks.append(
            QualityCheck(
                name="no_answer_accuracy",
                passed=(
                    no_answer_accuracy is None
                    or no_answer_accuracy >= selected.min_no_answer_accuracy
                ),
                measured=no_answer_accuracy,
                required=f">={selected.min_no_answer_accuracy}",
                detail=(
                    "not applicable: dataset has no no-answer cases"
                    if no_answer_accuracy is None
                    else None
                ),
            )
        )

        valid_citations = 0
        hit_count = 0
        for case in evaluation.results:
            for hit in case.retrieval.hits:
                hit_count += 1
                try:
                    citation = citation_from_hit(hit)
                    verify_citation(citation, hit)
                except ValueError:
                    continue
                valid_citations += 1
        citation_coverage = None if hit_count == 0 else valid_citations / hit_count
        checks.append(
            QualityCheck(
                name="retrieval_evidence_citation_coverage",
                passed=(
                    citation_coverage is not None
                    and citation_coverage >= selected.min_citation_coverage
                ),
                measured=citation_coverage,
                required=f">={selected.min_citation_coverage}",
                detail=(
                    "retrieval evidence citable; this does not measure generated-answer correctness"
                    if citation_coverage is not None
                    else "undefined: retrieval returned no evidence hits"
                ),
            )
        )

        stale_hits = sum(
            hit.status is not KnowledgeUnitStatus.ACTIVE
            for case in evaluation.results
            for hit in case.retrieval.hits
        )
        checks.append(
            QualityCheck(
                name="stale_or_retired_hits",
                passed=stale_hits <= selected.max_stale_hits,
                measured=stale_hits,
                required=f"<={selected.max_stale_hits}",
            )
        )
        include_stale_count = sum(item.include_stale for item in evaluation.dataset.items)
        checks.append(
            QualityCheck(
                name="stale_isolation_configuration",
                passed=selected.allow_include_stale or include_stale_count == 0,
                measured=include_stale_count,
                required="0 include_stale cases" if not selected.allow_include_stale else "allowed",
                detail="default acceptance fails closed when a case enables stale retrieval",
            )
        )
        checks.append(
            QualityCheck(
                name="retrieval_degradation",
                passed=selected.allow_degraded or not evaluation.degraded,
                measured=evaluation.degraded,
                required="false" if not selected.allow_degraded else "allowed",
            )
        )

        if baseline is not None:
            try:
                comparison = compare_baseline(baseline, evaluation)
                gate = evaluate_gate(comparison, regression_policy)
                checks.append(
                    QualityCheck(
                        name="regression_gate",
                        passed=gate.passed,
                        measured=gate.passed,
                        required="pass",
                        detail="; ".join(gate.violations) or None,
                    )
                )
            except (TypeError, ValueError) as error:
                checks.append(
                    QualityCheck(
                        name="regression_gate",
                        passed=False,
                        measured=None,
                        required="compatible healthy baseline",
                        detail=str(error),
                    )
                )
        elif regression_policy is not None:
            checks.append(
                QualityCheck(
                    name="regression_gate",
                    passed=False,
                    measured=None,
                    required="baseline when regression policy is supplied",
                    detail="regression policy supplied without a baseline",
                )
            )

        violations = tuple(
            f"{check.name}: {check.detail or f'measured {check.measured!r}, req {check.required}'}"
            for check in checks
            if not check.passed
        )
        return QualityReport(
            passed=not violations,
            baseline=snapshot,
            metrics=evaluation.metrics,
            checks=tuple(checks),
            citation_coverage=citation_coverage,
            stale_hits=stale_hits,
            degraded=evaluation.degraded,
            violations=violations,
        )
