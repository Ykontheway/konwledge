"""Portable database schema collection, snapshots, diffs, and publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from konwledge.database_schema.collector import (
    PostgreSQLSchemaCollector,
    PostgresSchemaCollector,
    SchemaCollector,
    SQLiteSchemaCollector,
    SqliteSchemaCollector,
)
from konwledge.database_schema.models import (
    MAX_COLUMNS,
    MAX_SNAPSHOT_BYTES,
    MAX_TABLES,
    SCHEMA_VERSION,
    SENSITIVE_METADATA_WARNING,
    ColumnChange,
    DatabaseCaptureResult,
    DatabaseColumn,
    DatabaseConnectionError,
    DatabaseDialect,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseNamespace,
    DatabaseObjectType,
    DatabaseSchemaDiff,
    DatabaseSchemaError,
    DatabaseSchemaLimitError,
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
    DatabaseTable,
    IndexTerm,
    TableChange,
    diff_database_schemas,
    dump_database_schema_json,
    load_database_schema_json,
)

if TYPE_CHECKING:
    from konwledge.database_schema.service import (
        DatabaseSchemaService,
        render_database_schema,
    )
    from konwledge.storage.database_schema import DatabaseSchemaSnapshotRepository

__all__ = [
    "MAX_COLUMNS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_TABLES",
    "SCHEMA_VERSION",
    "SENSITIVE_METADATA_WARNING",
    "ColumnChange",
    "DatabaseCaptureResult",
    "DatabaseColumn",
    "DatabaseConnectionError",
    "DatabaseDialect",
    "DatabaseForeignKey",
    "DatabaseIndex",
    "DatabaseNamespace",
    "DatabaseObjectType",
    "DatabaseSchemaDiff",
    "DatabaseSchemaError",
    "DatabaseSchemaLimitError",
    "DatabaseSchemaService",
    "DatabaseSchemaSnapshot",
    "DatabaseSchemaSnapshotRecord",
    "DatabaseSchemaSnapshotRepository",
    "DatabaseTable",
    "IndexTerm",
    "PostgreSQLSchemaCollector",
    "PostgresSchemaCollector",
    "SQLiteSchemaCollector",
    "SchemaCollector",
    "SqliteSchemaCollector",
    "TableChange",
    "diff_database_schemas",
    "dump_database_schema_json",
    "load_database_schema_json",
    "render_database_schema",
]


def __getattr__(name: str) -> Any:
    if name in {"DatabaseSchemaService", "render_database_schema"}:
        from konwledge.database_schema import service

        return getattr(service, name)
    if name == "DatabaseSchemaSnapshotRepository":
        from konwledge.storage.database_schema import DatabaseSchemaSnapshotRepository

        return DatabaseSchemaSnapshotRepository
    raise AttributeError(name)
