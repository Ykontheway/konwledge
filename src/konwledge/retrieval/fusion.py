"""Deterministic reciprocal rank fusion, independent of raw score scales."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RankedUnit:
    """One unit in a route's already ranked output; score is diagnostic only."""

    unit_id: UUID
    score: float


@dataclass(frozen=True, slots=True)
class FusedUnit:
    """RRF score together with the first occurrence on each route."""

    unit_id: UUID
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    lexical_score: float | None = None
    vector_score: float | None = None


def _unique_ranks(ranking: Iterable[RankedUnit]) -> dict[UUID, tuple[int, float]]:
    ranked: dict[UUID, tuple[int, float]] = {}
    for hit in ranking:
        if hit.unit_id not in ranked:
            ranked[hit.unit_id] = (len(ranked) + 1, hit.score)
    return ranked


def fuse_rankings(
    lexical: Iterable[RankedUnit],
    vector: Iterable[RankedUnit],
    *,
    rrf_k: int = 60,
) -> tuple[FusedUnit, ...]:
    """Deduplicate each route, assign 1-based ranks, then sum 1/(k + rank).

    Input order, not raw BM25/cosine magnitude, defines each route's ranking.
    Duplicate ids retain their first raw score and do not consume another rank.
    Single-route results use the same RRF scoring. UUID ascending breaks ties.
    """

    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
        raise ValueError("rrf_k must be a positive integer")
    lexical_ranks = _unique_ranks(lexical)
    vector_ranks = _unique_ranks(vector)
    fused: list[FusedUnit] = []
    for unit_id in lexical_ranks.keys() | vector_ranks.keys():
        lexical_entry = lexical_ranks.get(unit_id)
        vector_entry = vector_ranks.get(unit_id)
        score = 0.0
        if lexical_entry is not None:
            score += 1.0 / (rrf_k + lexical_entry[0])
        if vector_entry is not None:
            score += 1.0 / (rrf_k + vector_entry[0])
        fused.append(
            FusedUnit(
                unit_id=unit_id,
                score=score,
                lexical_rank=None if lexical_entry is None else lexical_entry[0],
                vector_rank=None if vector_entry is None else vector_entry[0],
                lexical_score=None if lexical_entry is None else lexical_entry[1],
                vector_score=None if vector_entry is None else vector_entry[1],
            )
        )
    return tuple(sorted(fused, key=lambda hit: (-hit.score, hit.unit_id)))
