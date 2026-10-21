"""Embedding provider protocol and a deterministic offline test provider.

The :class:`EmbeddingProvider` protocol captures the minimal surface the
knowledge-ci pipeline requires from an embedding backend:

* a stable ``name`` plus the specific ``model`` identifier (both used to form
  cache keys and to detect accidental backend swaps),
* a fixed output ``dimension`` so that storage layers can allocate space and
  check for drift, and
* an ``embed`` method that turns a batch of texts into unit-length vectors.

The provider shipped with the library, :class:`HashEmbeddingProvider`, is a
deterministic SHA-256 based implementation. It never touches the network, has
no credentials, and always produces the same vector for the same ``(model,
text)`` pair, which makes it a reliable fixture for offline tests and
development.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

EmbeddingVector = tuple[float, ...]
"""Immutable ordered vector of floats used across the embedding layer."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal protocol every embedding backend must satisfy."""

    @property
    def name(self) -> str:
        """Short, stable identifier for the backend family (e.g. ``"hash"``)."""

    @property
    def model(self) -> str:
        """Specific model identifier within the backend family."""

    @property
    def dimension(self) -> int:
        """Fixed vector dimension produced by :meth:`embed`."""

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        """Return one vector per input text, preserving order."""


class HashEmbeddingProvider:
    """Deterministic L2-normalised hash embedding provider for offline testing.

    The provider derives its vectors solely from ``model`` and ``text`` using
    SHA-256. It therefore satisfies three key properties:

    * **Deterministic** - identical inputs always yield identical vectors,
      which keeps regression tests stable.
    * **Offline** - no network access, API keys, or heavyweight model weights.
    * **Normalised** - each returned vector has unit length, so dot product
      doubles as cosine similarity for downstream indexing layers.
    """

    _NAME = "hash"
    _DEFAULT_DIMENSION = 32

    def __init__(self, *, model: str = "sha256-v1", dimension: int = _DEFAULT_DIMENSION) -> None:
        if not model or not model.strip():
            raise ValueError("model identifier must be a non-empty string")
        if dimension <= 0:
            raise ValueError("dimension must be a positive integer")
        self._model = model
        self._dimension = dimension

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        """Return deterministic unit vectors, one per input text."""

        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> EmbeddingVector:
        seed = f"{self._model}\x1f{text}".encode()
        values: list[float] = []
        counter = 0
        while len(values) < self._dimension:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(block), 4):
                if len(values) >= self._dimension:
                    break
                chunk = block[offset : offset + 4]
                raw = int.from_bytes(chunk, "big", signed=False)
                # Map uint32 -> [-1, 1) with a stable linear transform.
                values.append((raw / 0xFFFFFFFF) * 2.0 - 1.0)
            counter += 1

        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            # Fallback for the astronomically unlikely all-zero draw.
            return tuple(1.0 / math.sqrt(self._dimension) for _ in range(self._dimension))
        return tuple(value / norm for value in values)
