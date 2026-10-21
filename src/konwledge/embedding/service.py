"""Batching and cache-aware service built on top of :class:`EmbeddingProvider`.

The service turns a backend into a safe, reusable utility by adding:

* **Batching** - inputs are split into fixed-size chunks before hitting the
  provider so that remote backends respect per-request limits and local
  providers avoid pathological single-call payloads.
* **Caching** - a provider/model-aware cache key is computed per text and
  cached vectors are reused across calls, keeping downstream pipelines
  idempotent and cheap.
* **Dimension checks** - every returned batch is validated so a silently
  swapped backend never corrupts persisted vectors.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from konwledge.embedding.providers import EmbeddingProvider, EmbeddingVector

_CACHE_KEY_SEPARATOR = "\x1f"


def embedding_cache_key(provider_name: str, model: str, text: str) -> str:
    """Return a stable cache key for ``(provider, model, text)``.

    The key is a SHA-256 hex digest over a null-safe composition of the three
    identifiers, so cache entries never collide across providers or model
    revisions even when the underlying text happens to match.
    """

    if not provider_name:
        raise ValueError("provider_name must be a non-empty string")
    if not model:
        raise ValueError("model must be a non-empty string")
    payload = _CACHE_KEY_SEPARATOR.join((provider_name, model, text))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EmbeddingCache(Protocol):
    """Minimal mapping-like interface used by :class:`EmbeddingService`."""

    def get(self, key: str) -> EmbeddingVector | None:
        """Return the vector cached under ``key`` or ``None`` on a miss."""

    def set(self, key: str, vector: EmbeddingVector) -> None:
        """Store ``vector`` under ``key`` for later reuse."""


class InMemoryEmbeddingCache:
    """Simple in-process cache suitable for tests and single-run pipelines."""

    def __init__(self) -> None:
        self._store: dict[str, EmbeddingVector] = {}

    def get(self, key: str) -> EmbeddingVector | None:
        return self._store.get(key)

    def set(self, key: str, vector: EmbeddingVector) -> None:
        self._store[key] = vector

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store


class EmbeddingService:
    """Batch + cache wrapper around a provider that preserves input order."""

    _DEFAULT_BATCH_SIZE = 16

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        cache: EmbeddingCache | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self._provider = provider
        self._cache = cache
        self._batch_size = batch_size

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def cache_key(self, text: str) -> str:
        """Return the cache key the service would use for ``text``."""

        return embedding_cache_key(self._provider.name, self._provider.model, text)

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        """Embed ``texts`` using the configured provider, batching + cache."""

        results: list[EmbeddingVector | None] = [None] * len(texts)
        pending: list[tuple[int, str, str]] = []
        for index, text in enumerate(texts):
            key = self.cache_key(text)
            cached = self._cache.get(key) if self._cache is not None else None
            if cached is not None:
                results[index] = cached
                continue
            pending.append((index, key, text))

        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            vectors = self._provider.embed([text for _, _, text in batch])
            if len(vectors) != len(batch):
                raise RuntimeError(
                    "embedding provider returned "
                    f"{len(vectors)} vectors for a batch of {len(batch)} texts"
                )
            for (index, key, _), vector in zip(batch, vectors, strict=True):
                if len(vector) != self._provider.dimension:
                    raise RuntimeError(
                        "embedding provider returned a vector of length "
                        f"{len(vector)}, expected {self._provider.dimension}"
                    )
                results[index] = vector
                if self._cache is not None:
                    self._cache.set(key, vector)

        # Narrow optional type: every slot must be populated at this point.
        final: list[EmbeddingVector] = []
        for index, slot in enumerate(results):
            if slot is None:  # pragma: no cover - defensive invariant
                raise RuntimeError(f"embedding for input index {index} is missing")
            final.append(slot)
        return final
