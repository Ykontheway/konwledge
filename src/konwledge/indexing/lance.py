"""Optional LanceDB adapter for the vector index layer.

LanceDB is a storage-first vector database with a Rust core, Arrow-based
storage format, and native cosine search. It is a sensible production target
for the knowledge-ci pipeline, but we treat it as an optional dependency:

* the module always imports (no top-level ``lancedb`` import), so the rest of
  the package stays installable in minimal CI environments;
* :class:`LanceVectorIndex` raises a clear :class:`ImportError` the first time
  it is instantiated without ``lancedb`` present, naming the extra to install;
* when LanceDB is available, the adapter implements the full
  :class:`~konwledge.indexing.base.VectorIndex` protocol by delegating to a
  single Lance table keyed by ``id``.

The adapter stores records as rows of ``{id: str, vector: list[float],
payload_json: str}`` so that arbitrary JSON-serialisable payloads survive a
round trip without requiring a fixed schema per project.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konwledge.embedding import EmbeddingVector
from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorRecord,
    matches_filter,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static type checking
    import lancedb  # type: ignore[import-not-found]


_INSTALL_HINT = (
    "LanceDB is an optional dependency. Install it with "
    "'pip install lancedb pyarrow' before using LanceVectorIndex."
)


class LanceVectorIndex:
    """VectorIndex backend backed by a single LanceDB table.

    Parameters
    ----------
    uri:
        Filesystem path (or Lance URI) where the database lives. The directory
        is created on first use.
    table_name:
        Name of the Lance table that stores the knowledge-ci vectors.
    dimension:
        Fixed vector dimension; must match the embedding provider in use.
    """

    def __init__(
        self,
        *,
        uri: str | Path,
        table_name: str = "knowledge_vectors",
        dimension: int,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be a positive integer")
        if not table_name or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._uri = str(uri)
        self._table_name = table_name
        self._dimension = dimension
        self._db = self._connect(self._uri)
        self._table = self._open_or_create_table()

    @property
    def dimension(self) -> int:
        return self._dimension

    def __len__(self) -> int:
        return int(self._table.count_rows())

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        rows = [self._row_for(record) for record in records]
        if not rows:
            return
        # Delete-then-add keeps upsert semantics without depending on LanceDB's
        # evolving merge-insert API.
        quoted_ids = ", ".join(f"'{_escape_sql(record.id)}'" for record in records)
        self._table.delete(f"id IN ({quoted_ids})")
        self._table.add(rows)

    def delete(self, ids: Sequence[str]) -> int:
        unique_ids = [record_id for record_id in dict.fromkeys(ids)]
        if not unique_ids:
            return 0
        existing = {row["id"] for row in self._table.search().to_list() if "id" in row}
        to_remove = [record_id for record_id in unique_ids if record_id in existing]
        if not to_remove:
            return 0
        quoted = ", ".join(f"'{_escape_sql(record_id)}'" for record_id in to_remove)
        self._table.delete(f"id IN ({quoted})")
        return len(to_remove)

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
        # Fetch more rows than top_k so Python-side metadata filtering still
        # yields enough results for the caller's requested limit.
        oversample = max(top_k * 4, top_k)
        raw_rows = self._table.search(list(query)).metric("cosine").limit(oversample).to_list()
        hits: list[SearchHit] = []
        for row in raw_rows:
            payload = _decode_payload(row.get("payload_json"))
            if not matches_filter(payload, filters):
                continue
            # LanceDB reports cosine distance (``1 - similarity``). Convert
            # back so scores align with the in-memory adapter.
            distance = float(row.get("_distance", 0.0))
            score = 1.0 - distance
            if min_score is not None and score < min_score:
                continue
            hits.append(SearchHit(id=str(row["id"]), score=score, payload=payload))
            if len(hits) >= top_k:
                break
        return hits

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        rows = [self._row_for(record) for record in records]
        self._db.drop_table(self._table_name, ignore_missing=True)
        self._table = self._db.create_table(self._table_name, data=rows or None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _row_for(self, record: VectorRecord) -> dict[str, Any]:
        if not record.id:
            raise ValueError("VectorRecord.id must be a non-empty string")
        if len(record.vector) != self._dimension:
            raise ValueError(
                "vector dimension mismatch: expected "
                f"{self._dimension}, got {len(record.vector)} for id {record.id!r}"
            )
        return {
            "id": record.id,
            "vector": list(record.vector),
            "payload_json": json.dumps(dict(record.payload), sort_keys=True, default=str),
        }

    def _connect(self, uri: str) -> lancedb.DBConnection:
        try:
            import lancedb
        except ImportError as exc:  # pragma: no cover - guarded by import check
            raise ImportError(_INSTALL_HINT) from exc
        path = Path(uri)
        path.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(path))

    def _open_or_create_table(self) -> Any:
        if self._table_name in self._db.table_names():
            return self._db.open_table(self._table_name)
        # LanceDB infers schema from the first batch; seed with a dummy row and
        # delete it so the vector column keeps the correct dimension.
        seed = {
            "id": "__seed__",
            "vector": [0.0] * self._dimension,
            "payload_json": json.dumps({}),
        }
        table = self._db.create_table(self._table_name, data=[seed])
        table.delete("id = '__seed__'")
        return table


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _decode_payload(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}
