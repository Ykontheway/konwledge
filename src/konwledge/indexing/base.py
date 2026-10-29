"""Core datatypes, protocol and filter helpers for the vector index layer.

A vector index exposes a small, backend-agnostic contract: callers push
:class:`VectorRecord` entries in, run top-K similarity queries with optional
metadata filters, and occasionally rebuild the index from a known-good source
of truth. Keeping these types pure Python (no numpy, no backend imports) makes
the protocol easy to satisfy for both the in-memory implementation and a
future LanceDB adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from konwledge.embedding import EmbeddingVector

MetadataValue = str | int | float | bool | None
"""Scalar metadata values that filters can compare against."""

MetadataFilter = Mapping[str, Any]
"""Simple metadata filter: ``{key: expected}`` or ``{key: {"in": [a, b]}}``.

Supported shapes per key:

* a scalar (``str``/``int``/``float``/``bool``/``None``) -- strict equality;
* ``{"in": iterable}`` -- membership in an allow-list;
* ``{"not": iterable}`` -- membership in a deny-list.

Collections stored on the record (``list``/``tuple``/``set``) are matched by
containment: a scalar matches when it appears in the collection; ``in`` and
``not`` match when any value overlaps.
"""


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """A single row in the vector index.

    Attributes
    ----------
    id:
        Stable string identifier. The adapter MUST treat two records with the
        same ``id`` as the same entity (upsert semantics).
    vector:
        Unit-length embedding vector produced by an
        :class:`~konwledge.embedding.EmbeddingProvider`.
    payload:
        Arbitrary JSON-serialisable metadata used for filtering and for
        hydrating search hits downstream.
    """

    id: str
    vector: EmbeddingVector
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result returned by :meth:`VectorIndex.search`.

    ``score`` is a cosine similarity in ``[-1.0, 1.0]``. Higher is better.
    """

    id: str
    score: float
    payload: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorIndex(Protocol):
    """Minimal operations every vector index backend must support."""

    @property
    def dimension(self) -> int:
        """Fixed vector dimension accepted by :meth:`upsert`."""

    def __len__(self) -> int:
        """Return the number of records currently persisted."""

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Insert or replace ``records``; MUST be idempotent per ``id``."""

    def delete(self, ids: Sequence[str]) -> int:
        """Delete records with the given ids; return the number removed."""

    def get(self, ids: Sequence[str]) -> dict[str, VectorRecord]:
        """Return stored records keyed by id; unknown ids are omitted."""

    def search(
        self,
        query: EmbeddingVector,
        *,
        top_k: int = 10,
        filters: MetadataFilter | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Return the top ``top_k`` records sorted by descending similarity."""

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        """Replace the full index contents with ``records`` atomically."""


def matches_filter(payload: Mapping[str, Any], filters: MetadataFilter | None) -> bool:
    """Return ``True`` when ``payload`` satisfies every clause in ``filters``."""

    if not filters:
        return True
    for key, expected in filters.items():
        actual = payload.get(key)
        if isinstance(expected, Mapping):
            if "in" in expected and not _match_in(actual, expected["in"]):
                return False
            if "not" in expected and _match_in(actual, expected["not"]):
                return False
            # Unknown operators are treated as a mismatch to fail loudly in tests.
            unknown = set(expected.keys()) - {"in", "not"}
            if unknown:
                raise ValueError(f"unsupported filter operators: {sorted(unknown)}")
            continue
        if not _match_scalar(actual, expected):
            return False
    return True


def _match_scalar(actual: Any, expected: MetadataValue) -> bool:
    if isinstance(actual, list | tuple | set | frozenset):
        return expected in actual
    return bool(actual == expected)


def _match_in(actual: Any, allowed: Iterable[Any]) -> bool:
    allow_list = list(allowed)
    if isinstance(actual, list | tuple | set | frozenset):
        return any(item in allow_list for item in actual)
    return actual in allow_list
