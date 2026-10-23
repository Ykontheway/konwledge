"""Vector indexing layer: records, filters, and backend adapters.

The indexing layer sits between the embedding service and any downstream
retrieval surface. It defines the minimal :class:`VectorIndex` protocol every
backend must satisfy and ships two adapters:

* :class:`InMemoryVectorIndex` -- a pure-Python, dependency-free backend that
  implements the full contract (upsert, delete, metadata filtering, top-K
  cosine search, and rebuild). It is the default used by tests and local
  pipelines.
* :class:`LanceVectorIndex` -- an optional adapter backed by LanceDB. The
  module is importable even when LanceDB is not installed; the adapter raises
  a descriptive error on instantiation if the dependency is missing.

The :class:`IndexingService` glues an embedding service and a vector index
together so that callers can turn :class:`~konwledge.domain.models.KnowledgeUnit`
objects into persisted vectors with a single call while keeping repeated runs
deterministic.
"""

from __future__ import annotations

from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorIndex,
    VectorRecord,
    matches_filter,
)
from konwledge.indexing.lance import LanceVectorIndex
from konwledge.indexing.memory import InMemoryVectorIndex
from konwledge.indexing.service import IndexingService, build_vector_record

__all__ = [
    "InMemoryVectorIndex",
    "IndexingService",
    "LanceVectorIndex",
    "MetadataFilter",
    "SearchHit",
    "VectorIndex",
    "VectorRecord",
    "build_vector_record",
    "matches_filter",
]
