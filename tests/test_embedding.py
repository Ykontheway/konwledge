"""Tests for the embedding provider, cache keys, and batching service."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from konwledge.embedding import (
    EmbeddingProvider,
    EmbeddingService,
    EmbeddingVector,
    HashEmbeddingProvider,
    InMemoryEmbeddingCache,
    embedding_cache_key,
)


class _RecordingProvider:
    """Instrumented provider that records every batch received."""

    def __init__(self, *, dimension: int = 4, model: str = "stub-v1") -> None:
        self._dimension = dimension
        self._model = model
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        batch = list(texts)
        self.calls.append(batch)
        return [self._vector_for(text) for text in batch]

    def _vector_for(self, text: str) -> EmbeddingVector:
        base = float(len(text) + 1)
        values = tuple((base + index) for index in range(self._dimension))
        norm = math.sqrt(sum(v * v for v in values))
        return tuple(v / norm for v in values)


class _BrokenDimensionProvider(_RecordingProvider):
    """Provider that silently returns a wrong-dimension vector."""

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        self.calls.append(list(texts))
        return [tuple(0.0 for _ in range(self._dimension + 1)) for _ in texts]


class _ShortBatchProvider(_RecordingProvider):
    """Provider that returns fewer vectors than requested."""

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        self.calls.append(list(texts))
        return []


def test_hash_provider_satisfies_protocol() -> None:
    provider = HashEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)
    assert provider.name == "hash"
    assert provider.dimension > 0


def test_hash_provider_is_deterministic_and_normalised() -> None:
    provider = HashEmbeddingProvider(dimension=8)
    first = provider.embed(["alpha", "beta"])
    second = provider.embed(["alpha", "beta"])
    assert first == second
    for vector in first:
        assert len(vector) == 8
        norm = math.sqrt(sum(value * value for value in vector))
        assert math.isclose(norm, 1.0, abs_tol=1e-9)


def test_hash_provider_distinguishes_texts_and_models() -> None:
    provider = HashEmbeddingProvider(dimension=16)
    alpha_v1, beta_v1 = provider.embed(["alpha", "beta"])
    assert alpha_v1 != beta_v1

    other_model = HashEmbeddingProvider(dimension=16, model="sha256-v2")
    (alpha_v2,) = other_model.embed(["alpha"])
    assert alpha_v1 != alpha_v2


def test_hash_provider_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        HashEmbeddingProvider(dimension=0)
    with pytest.raises(ValueError):
        HashEmbeddingProvider(model="   ")


def test_cache_key_depends_on_provider_model_and_text() -> None:
    base = embedding_cache_key("hash", "sha256-v1", "alpha")
    assert base == embedding_cache_key("hash", "sha256-v1", "alpha")
    assert base != embedding_cache_key("hash", "sha256-v2", "alpha")
    assert base != embedding_cache_key("hash", "sha256-v1", "beta")
    assert base != embedding_cache_key("other", "sha256-v1", "alpha")


def test_cache_key_rejects_empty_identifiers() -> None:
    with pytest.raises(ValueError):
        embedding_cache_key("", "sha256-v1", "alpha")
    with pytest.raises(ValueError):
        embedding_cache_key("hash", "", "alpha")


def test_service_batches_inputs_and_preserves_order() -> None:
    provider = _RecordingProvider()
    service = EmbeddingService(provider, batch_size=2)
    texts = ["one", "two", "three", "four", "five"]

    vectors = service.embed(texts)

    assert len(vectors) == len(texts)
    assert [len(batch) for batch in provider.calls] == [2, 2, 1]
    assert provider.calls[0] == ["one", "two"]
    assert provider.calls[-1] == ["five"]
    assert vectors[0] == provider._vector_for("one")
    assert vectors[-1] == provider._vector_for("five")


def test_service_reuses_cached_vectors() -> None:
    provider = _RecordingProvider()
    cache = InMemoryEmbeddingCache()
    service = EmbeddingService(provider, cache=cache, batch_size=4)

    first = service.embed(["alpha", "beta"])
    assert len(provider.calls) == 1
    assert len(cache) == 2
    assert service.cache_key("alpha") in cache

    second = service.embed(["alpha", "beta", "gamma"])
    assert second[0] == first[0]
    assert second[1] == first[1]
    # Only the uncached "gamma" should hit the provider on the second call.
    assert provider.calls[-1] == ["gamma"]
    assert len(cache) == 3


def test_service_handles_empty_input_without_calling_provider() -> None:
    provider = _RecordingProvider()
    service = EmbeddingService(provider)
    assert service.embed([]) == []
    assert provider.calls == []


def test_service_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError):
        EmbeddingService(HashEmbeddingProvider(), batch_size=0)


def test_service_detects_wrong_dimension() -> None:
    provider = _BrokenDimensionProvider()
    service = EmbeddingService(provider)
    with pytest.raises(RuntimeError, match="length"):
        service.embed(["alpha"])


def test_service_detects_short_batch_response() -> None:
    provider = _ShortBatchProvider()
    service = EmbeddingService(provider)
    with pytest.raises(RuntimeError, match="batch"):
        service.embed(["alpha", "beta"])
