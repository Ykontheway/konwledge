"""Embedding provider protocol, local test provider, batching and cache keys."""

from __future__ import annotations

from konwledge.embedding.providers import (
    EmbeddingProvider,
    EmbeddingVector,
    HashEmbeddingProvider,
)
from konwledge.embedding.service import (
    EmbeddingCache,
    EmbeddingService,
    InMemoryEmbeddingCache,
    embedding_cache_key,
)

__all__ = [
    "EmbeddingCache",
    "EmbeddingProvider",
    "EmbeddingService",
    "EmbeddingVector",
    "HashEmbeddingProvider",
    "InMemoryEmbeddingCache",
    "embedding_cache_key",
]
