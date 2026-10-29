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

The adapter stores records as rows of ``{id: str, vector: fixed_size_list<float32>,
payload_json: str}`` with an explicit Arrow schema, so empty tables keep their
vector dimension and arbitrary JSON-serialisable payloads survive a round trip.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from konwledge.embedding import EmbeddingVector
from konwledge.indexing.base import (
    MetadataFilter,
    SearchHit,
    VectorRecord,
    matches_filter,
)

_INSTALL_HINT = (
    "LanceDB is an optional dependency. Install it with "
    "'uv sync --extra lancedb' (or 'pip install \"konwledge[lancedb]\"') "
    "before using LanceVectorIndex."
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
        Fixed vector dimension; must match the embedding provider in use and
        any existing table at ``uri``.
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
        self._db: Any = self._connect(self._uri)
        self._schema: Any = self._build_schema()
        self._table: Any = self._open_or_create_table()

    @property
    def dimension(self) -> int:
        return self._dimension

    def __len__(self) -> int:
        return int(self._table.count_rows())

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        # Later records win on duplicate ids, matching the in-memory adapter.
        latest = {record.id: record for record in records}
        rows = [self._row_for(record) for record in latest.values()]
        if not rows:
            return
        # Delete-then-add keeps upsert semantics without depending on LanceDB's
        # evolving merge-insert API.
        self._table.delete(_id_predicate(latest))
        self._table.add(rows)

    def delete(self, ids: Sequence[str]) -> int:
        unique_ids = list(dict.fromkeys(ids))
        if not unique_ids:
            return 0
        predicate = _id_predicate(unique_ids)
        removed = int(self._table.count_rows(predicate))
        if removed:
            self._table.delete(predicate)
        return removed

    def get(self, ids: Sequence[str]) -> dict[str, VectorRecord]:
        unique_ids = list(dict.fromkeys(ids))
        if not unique_ids:
            return {}
        rows = self._table.search().where(_id_predicate(unique_ids)).limit(len(unique_ids))
        found: dict[str, VectorRecord] = {}
        for row in rows.to_list():
            record_id = str(row["id"])
            found[record_id] = VectorRecord(
                id=record_id,
                vector=tuple(float(value) for value in row["vector"]),
                payload=_decode_payload(row.get("payload_json")),
            )
        return {record_id: found[record_id] for record_id in unique_ids if record_id in found}

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
        total = len(self)
        if total == 0:
            return []
        # Metadata filters run in Python on the JSON payload, so filtered queries
        # scan every row; otherwise a matching record could fall outside a fixed
        # oversampling window and silently disappear from the results.
        limit = total if filters else min(total, top_k)
        raw_rows = self._table.search(list(query)).metric("cosine").limit(limit).to_list()
        hits: list[SearchHit] = []
        for row in raw_rows:
            payload = _decode_payload(row.get("payload_json"))
            if not matches_filter(payload, filters):
                continue
            # LanceDB reports cosine distance (``1 - similarity``). Convert
            # back so scores align with the in-memory adapter.
            score = 1.0 - float(row.get("_distance", 0.0))
            if min_score is not None and score < min_score:
                continue
            hits.append(SearchHit(id=str(row["id"]), score=score, payload=payload))
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return hits[:top_k]

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        latest = {record.id: record for record in records}
        rows = [self._row_for(record) for record in latest.values()]
        # ``mode="overwrite"`` commits a new table version in one step, so a
        # failed rebuild never leaves an empty or half-written table behind.
        self._table = self._db.create_table(
            self._table_name,
            data=rows or None,
            schema=self._schema,
            mode="overwrite",
        )

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

    def _connect(self, uri: str) -> Any:
        try:
            lancedb = importlib.import_module("lancedb")
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        path = Path(uri)
        path.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(path))

    def _build_schema(self) -> Any:
        pyarrow = importlib.import_module("pyarrow")
        return pyarrow.schema(
            [
                pyarrow.field("id", pyarrow.string(), nullable=False),
                pyarrow.field("vector", pyarrow.list_(pyarrow.float32(), self._dimension)),
                pyarrow.field("payload_json", pyarrow.string()),
            ]
        )

    def _open_or_create_table(self) -> Any:
        # LanceDB 0.40 raises ``ValueError`` both for a missing table and for a
        # schema mismatch inside ``create_table(exist_ok=True)``, and
        # ``list_tables()`` returns a paginated response object. Opening first
        # keeps the two cases apart so a dimension mismatch gets a clear error.
        try:
            table = self._db.open_table(self._table_name)
        except ValueError:
            return self._db.create_table(self._table_name, schema=self._schema)
        vector_type = table.schema.field("vector").type
        stored_dimension = getattr(vector_type, "list_size", None)
        if stored_dimension != self._dimension:
            raise ValueError(
                f"existing table {self._table_name!r} stores vectors of dimension "
                f"{stored_dimension}, expected {self._dimension}; rebuild it with a "
                "matching embedding provider or use another table name"
            )
        return table


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _id_predicate(ids: Sequence[str] | dict[str, Any]) -> str:
    quoted = ", ".join(f"'{_escape_sql(record_id)}'" for record_id in ids)
    return f"id IN ({quoted})"


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
