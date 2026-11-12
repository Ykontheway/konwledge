"""Installed 30-case/1000-active-unit acceptance and local performance runner."""

from __future__ import annotations

import hashlib
import math
import platform
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import fmean
from uuid import UUID

from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    Source,
    SourceKind,
)
from konwledge.evaluation import EvaluationService
from konwledge.lifecycle import LifecycleService, publish_document
from konwledge.quality.benchmark_corpus import (
    CREATED_AT,
    CURRENT_DOCUMENT_ID,
    CURRENT_EVIDENCE_BY_INDEX,
    EVALUATED_AT,
    FIRST_PUBLISHED_AT,
    HISTORICAL_DOCUMENT_ID,
    HISTORICAL_EVIDENCE_BY_INDEX,
    SECOND_PUBLISHED_AT,
    SOURCE_ID,
    STALE_INTERFERENCE_TEXT,
    STALE_MARKED_AT,
    STALE_UNIT_INDEX,
    build_evaluation_set,
    stable_id,
)
from konwledge.quality.models import QualityReport
from konwledge.quality.service import QualityService
from konwledge.retrieval import RetrievalMode, RetrievalService
from konwledge.storage import SessionFactory, UnitOfWork

ACTIVE_CORPUS_COUNT = 1000
TOTAL_CURRENT_UNIT_COUNT = ACTIVE_CORPUS_COUNT + 1
SAMPLE_COUNT = 30
WARMUP_COUNT = 3
P95_LIMIT_SECONDS = 1.5
PERFORMANCE_QUERY = "Where is the Atlas backup stored?"


@dataclass(frozen=True, slots=True)
class PerformanceResult:
    passed: bool
    p95_seconds: float
    min_seconds: float
    mean_seconds: float
    max_seconds: float
    python: str
    sqlite: str
    platform: str
    mode: str
    top_k: int
    corpus_count: int
    total_current_units: int
    stale_count: int
    sample_count: int
    warmup_count: int
    local_threshold_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "latency_seconds": {
                "p95": self.p95_seconds,
                "min": self.min_seconds,
                "mean": self.mean_seconds,
                "max": self.max_seconds,
            },
            "environment": {
                "python": self.python,
                "sqlite": self.sqlite,
                "platform": self.platform,
            },
            "configuration": {
                "mode": self.mode,
                "top_k": self.top_k,
                "active_corpus_count": self.corpus_count,
                "total_current_units": self.total_current_units,
                "stale_count": self.stale_count,
                "sample_count": self.sample_count,
                "warmup_count": self.warmup_count,
                "local_acceptance_threshold_seconds": self.local_threshold_seconds,
            },
            "scope": "local reproducible acceptance threshold; not a universal SLA",
        }


def _filler_text(index: int) -> str:
    return (
        f"Reference record {index:04d} covers routine inventory reconciliation. "
        f"Its unique catalog code is fillerrecord{index:04d}."
    )


def _units_for_document(
    document_id: UUID,
    evidence: dict[int, str],
    *,
    count: int | None = None,
    unit_kind: str,
) -> tuple[KnowledgeUnit, ...]:
    indexes: Sequence[int] = range(count) if count is not None else tuple(sorted(evidence))
    units: list[KnowledgeUnit] = []
    cursor = 0
    for ordinal, index in enumerate(indexes):
        text = evidence.get(index, _filler_text(index))
        units.append(
            KnowledgeUnit(
                id=stable_id(unit_kind, index),
                lineage_id=stable_id("lineage", index),
                document_id=document_id,
                ordinal=ordinal,
                text=text,
                start_char=cursor,
                end_char=cursor + len(text),
                heading=f"Acceptance evidence {index}",
                section_path=("quality acceptance", f"record {index}"),
                metadata={"benchmark_index": index},
            )
        )
        cursor += len(text) + 1
    return tuple(units)


def _checksum(units: Sequence[KnowledgeUnit]) -> str:
    body = "\n".join(unit.text for unit in units)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def populate_corpus(session_factory: SessionFactory) -> None:
    """Publish two real document heads, then mark one extra current unit stale.

    The current head stores 1,001 units.  Exactly 1,000 remain active after the
    explicit stale mark; historical document versions are not counted as active.
    """

    historical_units = _units_for_document(
        HISTORICAL_DOCUMENT_ID,
        HISTORICAL_EVIDENCE_BY_INDEX,
        unit_kind="historical-unit",
    )
    current_evidence = {
        **CURRENT_EVIDENCE_BY_INDEX,
        STALE_UNIT_INDEX: STALE_INTERFERENCE_TEXT,
    }
    current_units = _units_for_document(
        CURRENT_DOCUMENT_ID,
        current_evidence,
        count=TOTAL_CURRENT_UNIT_COUNT,
        unit_kind="current-unit",
    )
    source = Source(
        id=SOURCE_ID,
        kind=SourceKind.MANUAL,
        uri="manual://installed-quality-acceptance-v2",
        title="Installed quality acceptance corpus",
        created_at=CREATED_AT,
        metadata={"language": "zh-CN,en", "retrieval_scope": "lexical"},
    )
    historical_document = Document(
        id=HISTORICAL_DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator="quality-acceptance/history-v1.txt",
        content_type=DocumentContentType.TEXT,
        checksum=_checksum(historical_units),
        ingested_at=CREATED_AT,
        metadata={"version": 1, "unit_count": len(historical_units)},
    )
    current_document = Document(
        id=CURRENT_DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator="quality-acceptance/current-v2.txt",
        content_type=DocumentContentType.TEXT,
        checksum=_checksum(current_units),
        ingested_at=SECOND_PUBLISHED_AT,
        metadata={"version": 2, "unit_count": len(current_units)},
    )

    revision_number = iter(range(2))

    def revision_id() -> UUID:
        return stable_id("publication-revision", next(revision_number))

    with UnitOfWork(session_factory) as uow:
        uow.sources.add(source)
        uow.documents.add(historical_document)
        for unit in historical_units:
            uow.knowledge_units.add(unit)
        publish_document(
            uow,
            historical_document,
            previous_document_id=None,
            reverted=False,
            id_factory=revision_id,
            created_at=FIRST_PUBLISHED_AT,
        )
        uow.documents.add(current_document)
        for unit in current_units:
            uow.knowledge_units.add(unit)
        publish_document(
            uow,
            current_document,
            previous_document_id=HISTORICAL_DOCUMENT_ID,
            reverted=False,
            id_factory=revision_id,
            created_at=SECOND_PUBLISHED_AT,
        )
        uow.commit()

    LifecycleService(
        session_factory,
        id_factory=lambda: stable_id("stale-revision", STALE_UNIT_INDEX),
        clock=lambda: STALE_MARKED_AT,
    ).mark_stale(
        stable_id("current-unit", STALE_UNIT_INDEX),
        reason="deliberate stale interference used to prove default isolation",
        author="quality-acceptance",
    )


def run_quality(session_factory: SessionFactory) -> QualityReport:
    """Import the fixed evaluation set and run all 30 cases once."""

    retrieval = RetrievalService(session_factory, clock=lambda: EVALUATED_AT)
    evaluation = EvaluationService(session_factory, retrieval, clock=lambda: EVALUATED_AT)
    dataset = build_evaluation_set()
    evaluation.import_set(dataset)
    return QualityService(
        session_factory,
        retrieval,
        clock=lambda: EVALUATED_AT,
    ).run(dataset.id)


def nearest_rank_percentile(samples: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank percentile after strict finite-value validation."""

    if not samples:
        raise ValueError("latency samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a number")
    if not math.isfinite(percentile) or not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be finite and in (0, 1]")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in samples
    ):
        raise ValueError("latency samples must be finite non-negative numbers")
    ordered = sorted(float(value) for value in samples)
    rank = math.ceil(float(percentile) * len(ordered))
    return ordered[rank - 1]


def run_performance(
    session_factory: SessionFactory,
    *,
    samples: int = SAMPLE_COUNT,
    top_k: int = 5,
    p95_limit_seconds: float = P95_LIMIT_SECONDS,
    timer: Callable[[], float] = time.perf_counter,
) -> PerformanceResult:
    """Measure real lexical searches after warmup and enforce nearest-rank P95."""

    if isinstance(samples, bool) or not isinstance(samples, int) or samples < SAMPLE_COUNT:
        raise ValueError(f"samples must be an integer >= {SAMPLE_COUNT}")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if (
        isinstance(p95_limit_seconds, bool)
        or not isinstance(p95_limit_seconds, (int, float))
        or not math.isfinite(p95_limit_seconds)
        or p95_limit_seconds <= 0
    ):
        raise ValueError("p95_limit_seconds must be a finite positive number")

    snapshot = LifecycleService(session_factory, clock=lambda: EVALUATED_AT).snapshot(EVALUATED_AT)
    active_count = len(snapshot.publishable_units)
    counts = snapshot.counts()
    stale_count = counts["stale"]
    current_count = len(snapshot.effective_units)
    if active_count != ACTIVE_CORPUS_COUNT:
        raise ValueError(
            f"performance corpus must contain exactly {ACTIVE_CORPUS_COUNT} active units"
        )
    if current_count != TOTAL_CURRENT_UNIT_COUNT or stale_count != 1:
        raise ValueError(
            "performance corpus must contain 1,001 current units with exactly one stale"
        )

    retrieval = RetrievalService(session_factory, clock=lambda: EVALUATED_AT)
    for _ in range(WARMUP_COUNT):
        retrieval.search(PERFORMANCE_QUERY, mode=RetrievalMode.LEXICAL, top_k=top_k)

    elapsed: list[float] = []
    for _ in range(samples):
        started = timer()
        retrieval.search(PERFORMANCE_QUERY, mode=RetrievalMode.LEXICAL, top_k=top_k)
        elapsed.append(timer() - started)

    p95 = nearest_rank_percentile(elapsed, 0.95)
    return PerformanceResult(
        passed=p95 <= p95_limit_seconds,
        p95_seconds=p95,
        min_seconds=min(elapsed),
        mean_seconds=fmean(elapsed),
        max_seconds=max(elapsed),
        python=sys.version.split()[0],
        sqlite=sqlite3.sqlite_version,
        platform=platform.platform(),
        mode=RetrievalMode.LEXICAL.value,
        top_k=top_k,
        corpus_count=active_count,
        total_current_units=current_count,
        stale_count=stale_count,
        sample_count=len(elapsed),
        warmup_count=WARMUP_COUNT,
        local_threshold_seconds=float(p95_limit_seconds),
    )
