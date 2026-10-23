"""High-level service that embeds knowledge units and indexes the vectors.

The :class:`IndexingService` is the main entry point for turning a batch of
:class:`~konwledge.domain.models.KnowledgeUnit` records into persisted vectors
with reusable metadata:

* each unit's text is sent through an :class:`~konwledge.embedding.EmbeddingService`
  (so caching and batching stay centralised);
* the resulting vector is paired with a stable payload derived from the unit
  (``document_id``, ``ordinal``, ``status``, ``tags``, ``section_path`` and
  any user-supplied extras) and persisted via :class:`VectorIndex.upsert`;
* higher-level helpers expose top-K search by raw text and full-index rebuilds
  so pipelines can keep repeated runs deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from konwledge.domain.models import KnowledgeUnit
from konwledge.embedding import EmbeddingService
from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorIndex,
    VectorRecord,
)


def build_vector_record(
    unit: KnowledgeUnit,
    vector: tuple[float, ...],
    *,
    extra_payload: Mapping[str, Any] | None = None,
) -> VectorRecord:
    """Return a :class:`VectorRecord` for ``unit`` with a traceable payload."""

    payload: dict[str, Any] = {
        "document_id": str(unit.document_id),
        "ordinal": unit.ordinal,
        "status": unit.status.value,
        "heading": unit.heading,
        "section_path": list(unit.section_path),
        "tags": sorted(unit.tags),
        "start_char": unit.start_char,
        "end_char": unit.end_char,
    }
    if unit.metadata:
        payload["metadata"] = dict(unit.metadata)
    if extra_payload:
        payload.update(extra_payload)
    return VectorRecord(id=str(unit.id), vector=vector, payload=payload)


class IndexingService:
    """Embed knowledge units and keep a :class:`VectorIndex` in sync."""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        vector_index: VectorIndex,
    ) -> None:
        provider_dimension = embedding_service.provider.dimension
        if vector_index.dimension != provider_dimension:
            raise ValueError(
                "vector_index dimension does not match embedding provider: "
                f"{vector_index.dimension} vs {provider_dimension}"
            )
        self._embedding_service = embedding_service
        self._vector_index = vector_index

    @property
    def vector_index(self) -> VectorIndex:
        return self._vector_index

    @property
    def embedding_service(self) -> EmbeddingService:
        return self._embedding_service

    def index_units(
        self,
        units: Sequence[KnowledgeUnit],
        *,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> list[VectorRecord]:
        """Embed and upsert ``units`` into the backing vector index."""

        if not units:
            return []
        vectors = self._embedding_service.embed([unit.text for unit in units])
        records = [
            build_vector_record(unit, vector, extra_payload=extra_payload)
            for unit, vector in zip(units, vectors, strict=True)
        ]
        self._vector_index.upsert(records)
        return records

    def rebuild(
        self,
        units: Sequence[KnowledgeUnit],
        *,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> list[VectorRecord]:
        """Replace the entire index with vectors derived from ``units``."""

        if not units:
            self._vector_index.rebuild([])
            return []
        vectors = self._embedding_service.embed([unit.text for unit in units])
        records = [
            build_vector_record(unit, vector, extra_payload=extra_payload)
            for unit, vector in zip(units, vectors, strict=True)
        ]
        self._vector_index.rebuild(records)
        return records

    def remove_units(self, unit_ids: Iterable[Any]) -> int:
        """Delete the vectors associated with ``unit_ids``."""

        ids = [str(unit_id) for unit_id in unit_ids]
        if not ids:
            return 0
        return self._vector_index.delete(ids)

    def search(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        filters: MetadataFilter | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Embed ``query_text`` and run a top-K search against the index."""

        if not query_text or not query_text.strip():
            raise ValueError("query_text must contain non-whitespace content")
        [query_vector] = self._embedding_service.embed([query_text])
        return self._vector_index.search(
            query_vector,
            top_k=top_k,
            filters=filters,
            min_score=min_score,
        )
