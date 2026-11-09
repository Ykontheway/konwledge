"""SQLite FTS5 helpers for knowledge unit full-text search."""

from __future__ import annotations

import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

_FTS_TABLE = "knowledge_units_fts"

_CREATE_FTS_TABLE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_units_fts "
    "USING fts5(unit_id UNINDEXED, tokens, tokenize='unicode61')"
)

_CREATE_INSERT_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS knowledge_units_fts_ai
AFTER INSERT ON knowledge_units
BEGIN
    INSERT INTO knowledge_units_fts(rowid, unit_id, tokens)
    VALUES (NEW.rowid, NEW.id, knowledge_fts_text(NEW.text));
END
"""

_CREATE_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS knowledge_units_fts_au
AFTER UPDATE OF id, text ON knowledge_units
BEGIN
    UPDATE knowledge_units_fts
    SET unit_id = NEW.id,
        tokens = knowledge_fts_text(NEW.text)
    WHERE rowid = NEW.rowid;
END
"""

_CREATE_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS knowledge_units_fts_ad
AFTER DELETE ON knowledge_units
BEGIN
    DELETE FROM knowledge_units_fts
    WHERE rowid = OLD.rowid;
END
"""

_BACKFILL_FTS_SQL = """
INSERT INTO knowledge_units_fts(rowid, unit_id, tokens)
SELECT knowledge_units.rowid, knowledge_units.id, knowledge_fts_text(knowledge_units.text)
FROM knowledge_units
LEFT JOIN knowledge_units_fts ON knowledge_units_fts.rowid = knowledge_units.rowid
WHERE knowledge_units_fts.rowid IS NULL
"""

_REBUILD_FTS_SQL = """
INSERT INTO knowledge_units_fts(rowid, unit_id, tokens)
SELECT knowledge_units.rowid, knowledge_units.id, knowledge_fts_text(knowledge_units.text)
FROM knowledge_units
"""

_SEARCH_SQL = text(
    """
    SELECT unit_id, bm25(knowledge_units_fts) AS score
    FROM knowledge_units_fts
    WHERE knowledge_units_fts MATCH :query
    ORDER BY bm25(knowledge_units_fts) ASC, unit_id ASC
    """
)

_COUNT_UNITS_SQL = text("SELECT COUNT(*) FROM knowledge_units")
_DELETE_ALL_FTS_SQL = text("DELETE FROM knowledge_units_fts")


@dataclass(frozen=True, slots=True)
class FullTextHit:
    """One FTS hit using SQLite's raw ``bm25`` score (lower is better)."""

    unit_id: UUID
    score: float


class FullTextRepository:
    """Query and rebuild the SQLite FTS index bound to one ORM session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(
        self,
        query: str,
        *,
        eligible_ids: Collection[UUID],
        limit: int = 50,
    ) -> tuple[FullTextHit, ...]:
        """Search the FTS table, then filter by eligible ids before applying ``limit``."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        compiled = _compile_query(query)
        if compiled is None or not eligible_ids:
            return ()

        self._session.flush()
        eligible_hex = {unit_id.hex for unit_id in eligible_ids}
        result = self._session.execute(_SEARCH_SQL, {"query": compiled})
        try:
            hits: list[FullTextHit] = []
            for unit_id_hex, score in result:
                if unit_id_hex not in eligible_hex:
                    continue
                hits.append(
                    FullTextHit(
                        unit_id=UUID(hex=unit_id_hex),
                        score=float(cast(float, score)),
                    )
                )
                if len(hits) >= limit:
                    break
            return tuple(hits)
        finally:
            result.close()

    def rebuild(self) -> int:
        """Delete and rebuild the FTS index inside the session transaction."""

        self._session.flush()
        connection = self._session.connection()
        create_full_text_schema(connection)
        total = int(self._session.execute(_COUNT_UNITS_SQL).scalar_one())
        self._session.execute(_DELETE_ALL_FTS_SQL)
        self._session.execute(text(_REBUILD_FTS_SQL))
        return total


def create_full_text_schema(connection: Connection) -> None:
    """Create the FTS table, triggers, and any missing backfilled rows."""

    _exec_fts_sql(connection, _CREATE_FTS_TABLE_SQL)
    connection.exec_driver_sql(_CREATE_INSERT_TRIGGER_SQL)
    connection.exec_driver_sql(_CREATE_UPDATE_TRIGGER_SQL)
    connection.exec_driver_sql(_CREATE_DELETE_TRIGGER_SQL)
    connection.exec_driver_sql(_BACKFILL_FTS_SQL)


def fts_document(text: str) -> str:
    """Normalize text for FTS5: split contiguous CJK into single-character tokens."""

    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if _is_cjk(char):
            tokens.append(char)
            index += 1
            continue
        if _is_word_start(char):
            token, index = _consume_word(text, index)
            tokens.append(token)
            continue
        index += 1
    return " ".join(tokens)


def _compile_query(query: str) -> str | None:
    if not query.strip():
        raise ValueError("query must contain non-whitespace content")

    clauses: list[str] = []
    index = 0
    while index < len(query):
        char = query[index]
        if _is_cjk(char):
            run: list[str] = []
            while index < len(query) and _is_cjk(query[index]):
                run.append(query[index])
                index += 1
            clauses.append(_quote_fts_phrase(" ".join(run)))
            continue
        if _is_word_start(char):
            token, index = _consume_word(query, index)
            clauses.append(_quote_fts_phrase(token))
            continue
        index += 1

    if not clauses:
        return None
    return " AND ".join(clauses)


def _consume_word(text_value: str, start: int) -> tuple[str, int]:
    end = start + 1
    while end < len(text_value):
        char = text_value[end]
        if _is_cjk(char) or not _is_word_continue(char):
            break
        end += 1
    return text_value[start:end], end


def _quote_fts_phrase(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _is_word_start(char: str) -> bool:
    return char == "_" or (char.isalnum() and not _is_cjk(char))


def _is_word_continue(char: str) -> bool:
    return char == "_" or char.isalnum() or unicodedata.category(char).startswith("M")


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        codepoint == 0x3007
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B739
        or 0x2B740 <= codepoint <= 0x2B81D
        or 0x2B820 <= codepoint <= 0x2CEA1
        or 0x2CEB0 <= codepoint <= 0x2EBE0
        or 0x2EBF0 <= codepoint <= 0x2EE5D
    )


def _exec_fts_sql(connection: Connection, statement: str) -> None:
    try:
        connection.exec_driver_sql(statement)
    except OperationalError as exc:  # pragma: no cover - branch exercised in tests
        if _is_missing_fts5_error(exc):
            raise RuntimeError(
                "SQLite FTS5 is required for full-text search, but this SQLite build "
                "does not provide the fts5 module."
            ) from exc
        raise
    except SQLAlchemyError as exc:  # pragma: no cover - defensive fallback
        if _is_missing_fts5_error(exc):
            raise RuntimeError(
                "SQLite FTS5 is required for full-text search, but this SQLite build "
                "does not provide the fts5 module."
            ) from exc
        raise


def _is_missing_fts5_error(exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "fts5" in message and "no such module" in message
