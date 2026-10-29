"""Pure-Python in-memory implementation of :class:`VectorIndex`.

The implementation keeps records in a dictionary keyed by id so that upsert
and delete are O(1) and search is a deterministic linear scan over the stored
vectors. It validates vector dimensions up front, defends against zero-length
vectors (which make cosine similarity undefined), and exposes a stable sort
tie-break on ``id`` so the same input always produces the same ranking.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from konwledge.embedding import EmbeddingVector
from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorRecord,
    matches_filter,
)


class InMemoryVectorIndex:
    """Dict-backed vector index suitable for tests and single-process pipelines."""

    def __init__(self, *, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be a positive integer")
        self._dimension = dimension
        self._store: dict[str, VectorRecord] = {}

    @property
    def dimension(self) -> int:
        return self._dimension

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, record_id: object) -> bool:
        return record_id in self._store

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Insert or replace ``records``; later entries win on duplicate ids."""

        validated: list[VectorRecord] = []
        for record in records:
            if not record.id:
                raise ValueError("VectorRecord.id must be a non-empty string")
            if len(record.vector) != self._dimension:
                raise ValueError(
                    "vector dimension mismatch: expected "
                    f"{self._dimension}, got {len(record.vector)} for id {record.id!r}"
                )
            validated.append(record)
        for record in validated:
            # ``dict`` assignment gives us upsert semantics for free.
            self._store[record.id] = record

    def delete(self, ids: Sequence[str]) -> int:
        removed = 0
        for record_id in ids:
            if self._store.pop(record_id, None) is not None:
                removed += 1
        return removed

    def get(self, ids: Sequence[str]) -> dict[str, VectorRecord]:
        return {record_id: self._store[record_id] for record_id in ids if record_id in self._store}

    def search(
        self,
        query: EmbeddingVector,
        *,
        top_k: int = 10,
        filters: MetadataFilter | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if len(query) != self._dimension:
            raise ValueError(
                f"query dimension mismatch: expected {self._dimension}, got {len(query)}"
            )
        query_norm = _l2_norm(query)
        if query_norm == 0.0:
            return []
        hits: list[SearchHit] = []
        for record in self._store.values():
            if not matches_filter(record.payload, filters):
                continue
            score = _cosine_similarity(query, record.vector, query_norm=query_norm)
            if min_score is not None and score < min_score:
                continue
            hits.append(SearchHit(id=record.id, score=score, payload=dict(record.payload)))
        # Sort by descending score with a stable tie-break on id so repeated
        # queries over the same data always yield the exact same ordering.
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return hits[:top_k]

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        """Replace the current contents with ``records`` atomically.

        Validation happens before mutation so a bad batch cannot leave the
        index in a half-rebuilt state.
        """

        validated = list(records)
        for record in validated:
            if not record.id:
                raise ValueError("VectorRecord.id must be a non-empty string")
            if len(record.vector) != self._dimension:
                raise ValueError(
                    "vector dimension mismatch: expected "
                    f"{self._dimension}, got {len(record.vector)} for id {record.id!r}"
                )
        self._store.clear()
        for record in validated:
            self._store[record.id] = record

    def snapshot(self) -> list[VectorRecord]:
        """Return a stable, id-sorted list of the stored records."""

        return [self._store[key] for key in sorted(self._store)]


def _l2_norm(vector: EmbeddingVector) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _cosine_similarity(
    left: EmbeddingVector,
    right: EmbeddingVector,
    *,
    query_norm: float | None = None,
) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = query_norm if query_norm is not None else _l2_norm(left)
    right_norm = _l2_norm(right)
    denominator = left_norm * right_norm
    if denominator == 0.0:
        return 0.0
    return numerator / denominator
