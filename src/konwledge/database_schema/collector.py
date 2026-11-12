"""Secure read-only schema collection for SQLite and PostgreSQL."""

from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Inspector, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from konwledge.database_schema.models import (
    DatabaseColumn,
    DatabaseConnectionError,
    DatabaseDialect,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseNamespace,
    DatabaseObjectType,
    DatabaseSchemaError,
    DatabaseSchemaSnapshot,
    DatabaseTable,
    IndexTerm,
)

_SQLITE_OK = sqlite3.SQLITE_OK
_SQLITE_DENY = sqlite3.SQLITE_DENY
_SQLITE_CATALOGS = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)
_SQLITE_METADATA_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "table_list",
    }
)
_SYSTEM_POSTGRES_SCHEMAS = frozenset({"information_schema", "pg_catalog"})


class SchemaCollector(ABC):
    """Separate collector API useful without the knowledge-library service."""

    @abstractmethod
    def capture(self) -> DatabaseSchemaSnapshot:
        """Return one complete, deterministic schema-only capture."""


class SQLiteSchemaCollector(SchemaCollector):
    """Collect SQLite metadata through a read-only URI and catalog authorizer."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise DatabaseSchemaError("SQLite target does not exist")
        if not candidate.is_file():
            raise DatabaseSchemaError("SQLite target must be a regular file")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise DatabaseSchemaError("timeout_seconds must be a finite number between 0 and 60")
        try:
            timeout = float(timeout_seconds)
        except (OverflowError, ValueError):
            raise DatabaseSchemaError(
                "timeout_seconds must be a finite number between 0 and 60"
            ) from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise DatabaseSchemaError("timeout_seconds must be a finite number between 0 and 60")
        self.path = candidate.resolve(strict=True)
        self._timeout_seconds = timeout

    def capture(self) -> DatabaseSchemaSnapshot:
        # Quoting the absolute path is required for literal '?', '#', and '%' characters.
        uri = f"file:{quote(self.path.as_posix(), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=self._timeout_seconds,
                isolation_level=None,
            )
        except sqlite3.Error:
            raise DatabaseConnectionError("unable to open SQLite target read-only") from None
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            connection.set_authorizer(self._authorizer)
            # Bound pathological catalogs without returning a partial snapshot.
            deadline = time.monotonic() + self._timeout_seconds
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1_000)
            snapshot = self._collect(connection)
            connection.execute("ROLLBACK")
            return snapshot
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise DatabaseSchemaError("SQLite schema capture failed") from None
        finally:
            connection.close()

    @staticmethod
    def _authorizer(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del database, trigger
        if action == sqlite3.SQLITE_SELECT:
            return _SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return _SQLITE_OK if (first or second) == "like" else _SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION and first in {"BEGIN", "ROLLBACK"}:
            return _SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return _SQLITE_OK if first in _SQLITE_CATALOGS else _SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return _SQLITE_OK if first in _SQLITE_METADATA_PRAGMAS else _SQLITE_DENY
        return _SQLITE_DENY

    @staticmethod
    def _pragma(connection: sqlite3.Connection, name: str, argument: str) -> list[sqlite3.Row]:
        if name not in _SQLITE_METADATA_PRAGMAS:
            raise DatabaseSchemaError("unsupported metadata pragma")
        literal = argument.replace("'", "''")
        return list(connection.execute(f"PRAGMA {name}('{literal}')"))

    @staticmethod
    def _index_sql(connection: sqlite3.Connection, name: str) -> str | None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        if row is None or row["sql"] is None:
            return None
        return str(row["sql"])

    def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
        objects = connection.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY name, type"
        ).fetchall()
        tables = tuple(
            self._table(
                connection,
                str(row["name"]),
                str(row["type"]),
                None if row["sql"] is None else str(row["sql"]),
            )
            for row in objects
        )
        return DatabaseSchemaSnapshot(
            dialect=DatabaseDialect.SQLITE,
            namespaces=(DatabaseNamespace(name="main", tables=tables),),
        )

    def _table(
        self,
        connection: sqlite3.Connection,
        name: str,
        raw_type: str,
        definition: str | None,
    ) -> DatabaseTable:
        object_type = DatabaseObjectType.TABLE if raw_type == "table" else DatabaseObjectType.VIEW
        column_rows = self._pragma(connection, "table_xinfo", name)
        columns = tuple(
            DatabaseColumn(
                name=str(row["name"]),
                data_type=str(row["type"] or ""),
                nullable=not bool(row["notnull"]) and not bool(row["pk"]),
                default=None if row["dflt_value"] is None else str(row["dflt_value"]),
                ordinal=int(row["cid"]),
                primary_key_position=int(row["pk"]) or None,
                generated=int(row["hidden"]) in {2, 3},
                hidden=int(row["hidden"]) == 1,
            )
            for row in column_rows
        )

        indexes: list[DatabaseIndex] = []
        foreign_keys: list[DatabaseForeignKey] = []
        without_rowid: bool | None = None
        strict: bool | None = None
        if object_type is DatabaseObjectType.TABLE:
            for row in self._pragma(connection, "index_list", name):
                index_name = str(row["name"])
                term_rows = [
                    term
                    for term in self._pragma(connection, "index_xinfo", index_name)
                    if bool(term["key"])
                ]
                index_sql = self._index_sql(connection, index_name)
                parsed = None if index_sql is None else _parse_sqlite_index_sql(index_sql)
                if index_sql is not None and parsed is None:
                    raise DatabaseSchemaError("unable to parse SQLite index metadata completely")
                if parsed is None:
                    if bool(row["partial"]) or any(int(term["cid"]) == -2 for term in term_rows):
                        raise DatabaseSchemaError("SQLite index metadata is incomplete")
                    raw_terms: tuple[str, ...] = ()
                    predicate = None
                else:
                    raw_terms, predicate = parsed
                    sequence_numbers = [int(term["seqno"]) for term in term_rows]
                    if len(raw_terms) != len(term_rows) or sorted(sequence_numbers) != list(
                        range(len(term_rows))
                    ):
                        raise DatabaseSchemaError("SQLite index term metadata is inconsistent")
                terms = tuple(
                    IndexTerm(
                        name=None if term["name"] is None else str(term["name"]),
                        expression=(
                            raw_terms[int(term["seqno"])] if int(term["cid"]) == -2 else None
                        ),
                        descending=bool(term["desc"]),
                    )
                    for term in term_rows
                )
                indexes.append(
                    DatabaseIndex(
                        name=index_name,
                        unique=bool(row["unique"]),
                        terms=terms,
                        predicate=predicate,
                    )
                )
            indexes.sort(key=lambda index: index.name)

            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in self._pragma(connection, "foreign_key_list", name):
                grouped.setdefault(int(row["id"]), []).append(row)
            for identifier in sorted(grouped):
                rows = sorted(grouped[identifier], key=lambda row: int(row["seq"]))
                foreign_keys.append(
                    DatabaseForeignKey(
                        columns=tuple(str(row["from"]) for row in rows),
                        referred_namespace="main",
                        referred_table=str(rows[0]["table"]),
                        referred_columns=tuple(
                            None if row["to"] is None else str(row["to"]) for row in rows
                        ),
                        on_update=str(rows[0]["on_update"]),
                        on_delete=str(rows[0]["on_delete"]),
                    )
                )

            table_list = self._pragma(connection, "table_list", name)
            own = next((row for row in table_list if str(row["name"]) == name), None)
            if own is not None:
                without_rowid = bool(own["wr"])
                strict = bool(own["strict"])

        return DatabaseTable(
            namespace="main",
            name=name,
            object_type=object_type,
            columns=columns,
            indexes=tuple(indexes),
            foreign_keys=tuple(foreign_keys),
            view_definition=(definition if object_type is DatabaseObjectType.VIEW else None),
            without_rowid=without_rowid,
            strict=strict,
        )


def _parse_sqlite_index_sql(sql: str) -> tuple[tuple[str, ...], str | None] | None:
    """Extract index terms and an optional WHERE predicate without executing SQL."""

    opening = _find_sqlite_index_list(sql)
    if opening is None:
        return None
    parsed_list = _split_sqlite_index_list(sql, opening)
    if parsed_list is None:
        return None
    terms, closing = parsed_list

    suffix = _skip_sqlite_trivia(sql, closing + 1)
    if suffix is None:
        return None
    if suffix == len(sql):
        return terms, None
    if sql[suffix] == ";":
        end = _skip_sqlite_trivia(sql, suffix + 1)
        return (terms, None) if end == len(sql) else None
    if sql[suffix : suffix + 5].casefold() != "where":
        return None
    keyword_end = suffix + 5
    if keyword_end < len(sql) and (sql[keyword_end].isalnum() or sql[keyword_end] in "_$"):
        return None
    predicate = sql[keyword_end:].strip()
    if not predicate or not _is_complete_sqlite_fragment(predicate):
        return None
    return terms, predicate


def _find_sqlite_index_list(sql: str) -> int | None:
    position = 0
    while position < len(sql):
        character = sql[position]
        if character in "'\"`[":
            position = _skip_sqlite_quoted(sql, position)
            if position < 0:
                return None
            continue
        if sql.startswith("--", position):
            position = _skip_sqlite_line_comment(sql, position)
            continue
        if sql.startswith("/*", position):
            position = _skip_sqlite_block_comment(sql, position)
            if position < 0:
                return None
            continue
        if character == "(":
            return position
        if character == ")":
            return None
        position += 1
    return None


def _split_sqlite_index_list(sql: str, opening: int) -> tuple[tuple[str, ...], int] | None:
    terms: list[str] = []
    term_start = opening + 1
    depth = 1
    position = term_start
    while position < len(sql):
        character = sql[position]
        if character in "'\"`[":
            position = _skip_sqlite_quoted(sql, position)
            if position < 0:
                return None
            continue
        if sql.startswith("--", position):
            position = _skip_sqlite_line_comment(sql, position)
            continue
        if sql.startswith("/*", position):
            position = _skip_sqlite_block_comment(sql, position)
            if position < 0:
                return None
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                term = sql[term_start:position].strip()
                if not term:
                    return None
                terms.append(term)
                return tuple(terms), position
        elif character == "," and depth == 1:
            term = sql[term_start:position].strip()
            if not term:
                return None
            terms.append(term)
            term_start = position + 1
        position += 1
    return None


def _skip_sqlite_quoted(sql: str, opening: int) -> int:
    quote = sql[opening]
    closing = "]" if quote == "[" else quote
    position = opening + 1
    while position < len(sql):
        if sql[position] != closing:
            position += 1
            continue
        if position + 1 < len(sql) and sql[position + 1] == closing:
            position += 2
            continue
        return position + 1
    return -1


def _skip_sqlite_line_comment(sql: str, opening: int) -> int:
    newline = sql.find("\n", opening + 2)
    return len(sql) if newline < 0 else newline + 1


def _skip_sqlite_block_comment(sql: str, opening: int) -> int:
    closing = sql.find("*/", opening + 2)
    return -1 if closing < 0 else closing + 2


def _skip_sqlite_trivia(sql: str, start: int) -> int | None:
    position = start
    while position < len(sql):
        if sql[position].isspace():
            position += 1
        elif sql.startswith("--", position):
            position = _skip_sqlite_line_comment(sql, position)
        elif sql.startswith("/*", position):
            position = _skip_sqlite_block_comment(sql, position)
            if position < 0:
                return None
        else:
            break
    return position


def _is_complete_sqlite_fragment(sql: str) -> bool:
    depth = 0
    position = 0
    while position < len(sql):
        character = sql[position]
        if character in "'\"`[":
            position = _skip_sqlite_quoted(sql, position)
            if position < 0:
                return False
            continue
        if sql.startswith("--", position):
            position = _skip_sqlite_line_comment(sql, position)
            continue
        if sql.startswith("/*", position):
            position = _skip_sqlite_block_comment(sql, position)
            if position < 0:
                return False
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return False
        position += 1
    return depth == 0


# Compatibility spelling for callers that used the original class draft.
SqliteSchemaCollector = SQLiteSchemaCollector


class PostgreSQLSchemaCollector(SchemaCollector):
    """Collect one explicitly scoped PostgreSQL schema in a read-only transaction."""

    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        if not isinstance(dsn, str) or not dsn:
            raise DatabaseSchemaError("PostgreSQL URL must not be empty")
        try:
            parsed = make_url(dsn)
        except (TypeError, ValueError, SQLAlchemyError):
            raise DatabaseSchemaError("invalid PostgreSQL URL") from None
        if parsed.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise DatabaseSchemaError("PostgreSQL URL must use PostgreSQL with psycopg")
        if not schema or len(schema) > 1_024 or "\x00" in schema:
            raise DatabaseSchemaError("invalid PostgreSQL schema name")
        if schema in _SYSTEM_POSTGRES_SCHEMAS or schema.startswith("pg_"):
            raise DatabaseSchemaError("system PostgreSQL schemas are excluded")
        self._url = parsed.set(drivername="postgresql+psycopg", query={})
        self.schema = schema

    def capture(self) -> DatabaseSchemaSnapshot:
        try:
            engine = create_engine(
                self._url,
                poolclass=NullPool,
                connect_args={
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=5000 -c lock_timeout=5000",
                },
            )
        except (ImportError, ModuleNotFoundError):
            raise DatabaseConnectionError(
                "PostgreSQL capture requires the optional 'postgres' extra (psycopg 3)"
            ) from None
        try:
            with (
                engine.connect().execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                ) as connection,
                connection.begin(),
            ):
                if connection.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                    raise DatabaseConnectionError(
                        "PostgreSQL connection did not enter a read-only transaction"
                    )
                return self._collect(inspect(connection))
        except DatabaseSchemaError:
            raise
        except (ImportError, ModuleNotFoundError):
            raise DatabaseConnectionError(
                "PostgreSQL capture requires the optional 'postgres' extra (psycopg 3)"
            ) from None
        except SQLAlchemyError:
            raise DatabaseConnectionError("PostgreSQL schema capture failed") from None
        finally:
            engine.dispose()

    def _collect(self, inspector: Inspector) -> DatabaseSchemaSnapshot:
        tables = [
            self._object(inspector, name, DatabaseObjectType.TABLE)
            for name in sorted(inspector.get_table_names(schema=self.schema))
        ]
        tables.extend(
            self._object(inspector, name, DatabaseObjectType.VIEW)
            for name in sorted(inspector.get_view_names(schema=self.schema))
        )
        tables.sort(key=lambda table: (table.name, table.object_type.value))
        return DatabaseSchemaSnapshot(
            dialect=DatabaseDialect.POSTGRESQL,
            namespaces=(DatabaseNamespace(name=self.schema, tables=tuple(tables)),),
        )

    def _object(
        self, inspector: Inspector, name: str, object_type: DatabaseObjectType
    ) -> DatabaseTable:
        raw_columns = inspector.get_columns(name, schema=self.schema)
        primary_key = inspector.get_pk_constraint(name, schema=self.schema)
        primary_positions = {
            column: position
            for position, column in enumerate(primary_key.get("constrained_columns") or (), 1)
        }
        columns = tuple(
            DatabaseColumn(
                name=str(raw["name"]),
                data_type=str(raw["type"]),
                nullable=bool(raw.get("nullable", True)),
                default=None if raw.get("default") is None else str(raw["default"]),
                ordinal=ordinal,
                primary_key_position=primary_positions.get(str(raw["name"])),
                generated=raw.get("computed") is not None or raw.get("identity") is not None,
                comment=None if raw.get("comment") is None else str(raw["comment"]),
            )
            for ordinal, raw in enumerate(raw_columns)
        )

        indexes: list[DatabaseIndex] = []
        foreign_keys: list[DatabaseForeignKey] = []
        if object_type is DatabaseObjectType.TABLE:
            for raw in inspector.get_indexes(name, schema=self.schema):
                expressions = raw.get("expressions") or ()
                terms: list[IndexTerm] = []
                for position, column in enumerate(raw.get("column_names") or ()):
                    expression = expressions[position] if position < len(expressions) else None
                    terms.append(
                        IndexTerm(
                            name=None if column is None else str(column),
                            expression=None if expression is None else str(expression),
                        )
                    )
                dialect_options = raw.get("dialect_options") or {}
                indexes.append(
                    DatabaseIndex(
                        name=str(raw["name"]),
                        unique=bool(raw.get("unique", False)),
                        terms=tuple(terms),
                        predicate=_optional_text(dialect_options.get("postgresql_where")),
                    )
                )
            indexes.sort(key=lambda index: index.name)

            for raw_fk in inspector.get_foreign_keys(name, schema=self.schema):
                options = raw_fk.get("options") or {}
                foreign_keys.append(
                    DatabaseForeignKey(
                        name=_optional_text(raw_fk.get("name")),
                        columns=tuple(str(value) for value in raw_fk["constrained_columns"]),
                        referred_namespace=_optional_text(raw_fk.get("referred_schema")),
                        referred_table=str(raw_fk["referred_table"]),
                        referred_columns=tuple(
                            None if value is None else str(value)
                            for value in raw_fk["referred_columns"]
                        ),
                        on_update=_optional_text(options.get("onupdate")),
                        on_delete=_optional_text(options.get("ondelete")),
                    )
                )
            foreign_keys.sort(key=lambda item: (item.name or "", item.columns))

        comment = inspector.get_table_comment(name, schema=self.schema).get("text")
        view_definition = (
            inspector.get_view_definition(name, schema=self.schema)
            if object_type is DatabaseObjectType.VIEW
            else None
        )
        return DatabaseTable(
            namespace=self.schema,
            name=name,
            object_type=object_type,
            columns=columns,
            indexes=tuple(indexes),
            foreign_keys=tuple(foreign_keys),
            comment=_optional_text(comment),
            view_definition=_optional_text(view_definition),
        )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


PostgresSchemaCollector = PostgreSQLSchemaCollector
