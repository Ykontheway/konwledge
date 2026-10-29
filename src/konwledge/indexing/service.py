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
  so pipelines can keep repeated runs deterministic;
* :meth:`IndexingService.apply_diff` performs a *partial* rebuild after a
  document changes: only added or modified units are embedded, unchanged and
  moved units reuse their stored vectors, and vectors of the previous version
  are removed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from konwledge.domain.models import KnowledgeUnit
from konwledge.embedding import EmbeddingService, EmbeddingVector
from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorIndex,
    VectorRecord,
)
from konwledge.lifecycle import DocumentDiff


def build_vector_record(
    unit: KnowledgeUnit,
    vector: tuple[float, ...],
    *,
    extra_payload: Mapping[str, Any] | None = None,
) -> VectorRecord:
    """Return a :class:`VectorRecord` for ``unit`` with a traceable payload."""

    payload: dict[str, Any] = {
        "document_id": str(unit.document_id),
        "lineage_id": str(unit.lineage_id),
        "content_hash": unit.content_hash,
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


@dataclass(frozen=True, slots=True)
class IncrementalIndexReport:
    """What a partial rebuild did, so callers can prove only changes were recomputed."""

    embedded: tuple[str, ...]
    reused: tuple[str, ...]
    deleted: int

    @property
    def upserted(self) -> int:
        return len(self.embedded) + len(self.reused)


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

    def apply_diff(
        self,
        diff: DocumentDiff,
        *,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> IncrementalIndexReport:
        """Bring the index from the previous document version to the next one.

        * ``added``/``modified`` units are embedded;
        * ``unchanged``/``moved`` units copy the vector stored for their old
          unit id (falling back to embedding if it is missing from the index),
          while their payload is refreshed with the new position and ids;
        * every old unit id is deleted, so the index only serves the latest
          version of the document.
        """

        reusable_ids = [
            str(change.old.id)
            for change in diff.changes
            if change.old is not None and change.new is not None and not change.needs_embedding
        ]
        stored = self._vector_index.get(reusable_ids) if reusable_ids else {}

        to_embed: list[KnowledgeUnit] = []
        reused: dict[str, EmbeddingVector] = {}
        for change in diff.changes:
            if change.new is None:
                continue
            record = None if change.old is None else stored.get(str(change.old.id))
            if change.needs_embedding or record is None:
                to_embed.append(change.new)
            else:
                reused[str(change.new.id)] = record.vector

        vectors = self._embedding_service.embed([unit.text for unit in to_embed])
        embedded = {str(unit.id): vector for unit, vector in zip(to_embed, vectors, strict=True)}

        records = []
        for unit in diff.new_units:
            unit_id = str(unit.id)
            vector = embedded[unit_id] if unit_id in embedded else reused[unit_id]
            records.append(build_vector_record(unit, vector, extra_payload=extra_payload))

        new_ids = {record.id for record in records}
        stale_ids = [
            str(change.old.id)
            for change in diff.changes
            if change.old is not None and str(change.old.id) not in new_ids
        ]
        deleted = self._vector_index.delete(stale_ids) if stale_ids else 0
        if records:
            self._vector_index.upsert(records)
        return IncrementalIndexReport(
            embedded=tuple(embedded),
            reused=tuple(reused),
            deleted=deleted,
        )

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
