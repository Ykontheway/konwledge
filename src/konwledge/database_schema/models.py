"""Strict, portable models for schema-only database captures."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal[1] = 1
MAX_TABLES = 1_000
MAX_COLUMNS = 10_000
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024
MAX_NAME_LENGTH = 1_024
MAX_TEXT_LENGTH = 1_000_000
SENSITIVE_METADATA_WARNING = (
    "Schema metadata may contain sensitive comments or default SQL; handle snapshots accordingly."
)


class DatabaseSchemaError(ValueError):
    """Base error for rejected targets, payloads, and captures."""


class DatabaseSchemaLimitError(DatabaseSchemaError):
    """A complete capture exceeds a configured safety bound; partial data is never returned."""


class DatabaseConnectionError(DatabaseSchemaError):
    """A database could not be safely opened without exposing connection details."""


class DatabaseDialect(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class DatabaseObjectType(StrEnum):
    TABLE = "table"
    VIEW = "view"


class SchemaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class IndexTerm(SchemaModel):
    """One index term. Both values are null when the backend hides an expression."""

    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    expression: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    descending: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def reject_blank(self) -> Self:
        for value in (self.name, self.expression):
            if value is not None and not value:
                raise ValueError("index term values must not be empty")
        return self


class DatabaseIndex(SchemaModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    unique: bool = Field(strict=True)
    terms: tuple[IndexTerm, ...] = Field(max_length=MAX_COLUMNS)
    predicate: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class DatabaseForeignKey(SchemaModel):
    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    columns: tuple[str, ...] = Field(min_length=1, max_length=MAX_COLUMNS)
    referred_namespace: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    referred_table: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    referred_columns: tuple[str | None, ...] = Field(min_length=1, max_length=MAX_COLUMNS)
    on_update: str | None = Field(default=None, max_length=128)
    on_delete: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def aligned_columns(self) -> Self:
        if len(self.columns) != len(self.referred_columns):
            raise ValueError("foreign key local and referred columns must have equal length")
        return self


class DatabaseColumn(SchemaModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    data_type: str = Field(max_length=MAX_TEXT_LENGTH)
    nullable: bool = Field(strict=True)
    default: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    ordinal: int = Field(strict=True, ge=0, lt=MAX_COLUMNS)
    primary_key_position: int | None = Field(default=None, strict=True, ge=1, le=MAX_COLUMNS)
    generated: bool = Field(default=False, strict=True)
    hidden: bool = Field(default=False, strict=True)
    comment: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class DatabaseTable(SchemaModel):
    namespace: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    object_type: DatabaseObjectType
    columns: tuple[DatabaseColumn, ...] = Field(max_length=MAX_COLUMNS)
    indexes: tuple[DatabaseIndex, ...] = Field(default=(), max_length=MAX_COLUMNS)
    foreign_keys: tuple[DatabaseForeignKey, ...] = Field(default=(), max_length=MAX_COLUMNS)
    comment: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    view_definition: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    without_rowid: bool | None = Field(default=None, strict=True)
    strict: bool | None = Field(default=None, strict=True)

    @property
    def key(self) -> str:
        return json.dumps(
            [self.namespace, self.name, self.object_type.value],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        columns = tuple(sorted(self.columns, key=lambda column: column.ordinal))
        ordinals = [column.ordinal for column in columns]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("column ordinals must be contiguous and start at zero")
        _reject_duplicates((column.name for column in columns), "column name")
        _reject_duplicates((index.name for index in self.indexes), "index name")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(
            self, "indexes", tuple(sorted(self.indexes, key=lambda index: index.name))
        )
        object.__setattr__(
            self,
            "foreign_keys",
            tuple(
                sorted(
                    self.foreign_keys,
                    key=lambda foreign_key: (
                        foreign_key.name or "",
                        foreign_key.columns,
                        foreign_key.referred_table,
                    ),
                )
            ),
        )
        return self


class DatabaseNamespace(SchemaModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    tables: tuple[DatabaseTable, ...] = Field(max_length=MAX_TABLES)

    @model_validator(mode="after")
    def validate_tables(self) -> Self:
        if any(table.namespace != self.name for table in self.tables):
            raise ValueError("every table namespace must match its container")
        _reject_duplicates(((table.name, table.object_type) for table in self.tables), "table key")
        object.__setattr__(
            self,
            "tables",
            tuple(sorted(self.tables, key=lambda table: (table.name, table.object_type.value))),
        )
        return self


class DatabaseSchemaSnapshot(SchemaModel):
    """Schema-only payload. Collection time and target identity are intentionally external."""

    schema_version: Literal[1] = SCHEMA_VERSION
    dialect: DatabaseDialect
    namespaces: tuple[DatabaseNamespace, ...]

    @model_validator(mode="before")
    @classmethod
    def strict_schema_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and type(value.get("schema_version", SCHEMA_VERSION)) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_bounds_and_order(self) -> Self:
        _reject_duplicates((namespace.name for namespace in self.namespaces), "namespace name")
        table_count = sum(len(namespace.tables) for namespace in self.namespaces)
        column_count = sum(
            len(table.columns) for namespace in self.namespaces for table in namespace.tables
        )
        if table_count > MAX_TABLES:
            raise DatabaseSchemaLimitError(f"schema has more than {MAX_TABLES} tables")
        if column_count > MAX_COLUMNS:
            raise DatabaseSchemaLimitError(f"schema has more than {MAX_COLUMNS} columns")
        object.__setattr__(
            self, "namespaces", tuple(sorted(self.namespaces, key=lambda item: item.name))
        )
        if len(self.canonical_json().encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise DatabaseSchemaLimitError(f"schema snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class DatabaseSchemaSnapshotRecord(SchemaModel):
    source_id: UUID
    version: int = Field(strict=True, ge=1)
    snapshot: DatabaseSchemaSnapshot
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    document_id: UUID

    @field_validator("captured_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("captured_at must be an ISO 8601 timestamp") from exc
        if not isinstance(value, datetime):
            raise ValueError("captured_at must be an ISO 8601 timestamp")
        return value

    @field_validator("captured_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include timezone information")
        return value

    @model_validator(mode="after")
    def matching_hash(self) -> Self:
        if self.content_hash != self.snapshot.content_hash:
            raise ValueError("content_hash does not match snapshot payload")
        return self


class ColumnChange(SchemaModel):
    name: str
    before: DatabaseColumn | None = None
    after: DatabaseColumn | None = None


class TableChange(SchemaModel):
    namespace: str
    name: str
    object_type: DatabaseObjectType
    added_columns: tuple[str, ...] = ()
    removed_columns: tuple[str, ...] = ()
    changed_columns: tuple[ColumnChange, ...] = ()
    indexes_changed: bool = Field(default=False, strict=True)
    constraints_changed: bool = Field(default=False, strict=True)
    comment_changed: bool = Field(default=False, strict=True)
    options_changed: bool = Field(default=False, strict=True)


class DatabaseSchemaDiff(SchemaModel):
    added_tables: tuple[str, ...] = ()
    removed_tables: tuple[str, ...] = ()
    changed_tables: tuple[TableChange, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not (self.added_tables or self.removed_tables or self.changed_tables)


class DatabaseCaptureResult(SchemaModel):
    source_id: UUID
    document_id: UUID
    version: int = Field(strict=True, ge=1)
    content_hash: str
    captured_at: datetime
    created: bool = Field(strict=True)
    reverted: bool = Field(default=False, strict=True)
    schema_same: bool = Field(strict=True)
    snapshot: DatabaseSchemaSnapshot
    diff: DatabaseSchemaDiff | None = None
    warnings: tuple[str, ...] = (SENSITIVE_METADATA_WARNING,)


def dump_database_schema_json(snapshot: DatabaseSchemaSnapshot, *, indent: int | None = 2) -> str:
    """Serialize a portable schema payload with deterministic keys and UTF-8 text."""

    if indent is None:
        return snapshot.canonical_json()
    return (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            allow_nan=False,
        )
        + "\n"
    )


def load_database_schema_json(payload: str | bytes | bytearray) -> DatabaseSchemaSnapshot:
    """Parse strict JSON, rejecting duplicate object keys and non-finite numbers."""

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DatabaseSchemaError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise DatabaseSchemaError(f"non-finite JSON number is not allowed: {value}")

    try:
        raw = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DatabaseSchemaError("invalid database schema JSON") from exc
    return DatabaseSchemaSnapshot.model_validate(raw)


def diff_database_schemas(
    before: DatabaseSchemaSnapshot, after: DatabaseSchemaSnapshot
) -> DatabaseSchemaDiff:
    """Return a deterministic, identity-based schema diff; renames are not guessed."""

    old = {table.key: table for namespace in before.namespaces for table in namespace.tables}
    new = {table.key: table for namespace in after.namespaces for table in namespace.tables}
    changed: list[TableChange] = []
    for key in sorted(old.keys() & new.keys()):
        left, right = old[key], new[key]
        left_columns = {column.name: column for column in left.columns}
        right_columns = {column.name: column for column in right.columns}
        added_columns = tuple(sorted(right_columns.keys() - left_columns.keys()))
        removed_columns = tuple(sorted(left_columns.keys() - right_columns.keys()))
        changed_columns = tuple(
            ColumnChange(name=name, before=left_columns[name], after=right_columns[name])
            for name in sorted(left_columns.keys() & right_columns.keys())
            if left_columns[name] != right_columns[name]
        )
        table_change = TableChange(
            namespace=right.namespace,
            name=right.name,
            object_type=right.object_type,
            added_columns=added_columns,
            removed_columns=removed_columns,
            changed_columns=changed_columns,
            indexes_changed=left.indexes != right.indexes,
            constraints_changed=left.foreign_keys != right.foreign_keys,
            comment_changed=left.comment != right.comment,
            options_changed=(left.without_rowid, left.strict, left.view_definition)
            != (right.without_rowid, right.strict, right.view_definition),
        )
        if any(
            (
                table_change.added_columns,
                table_change.removed_columns,
                table_change.changed_columns,
                table_change.indexes_changed,
                table_change.constraints_changed,
                table_change.comment_changed,
                table_change.options_changed,
            )
        ):
            changed.append(table_change)
    return DatabaseSchemaDiff(
        added_tables=tuple(sorted(new.keys() - old.keys())),
        removed_tables=tuple(sorted(old.keys() - new.keys())),
        changed_tables=tuple(changed),
    )


def _reject_duplicates(values: Any, label: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def ensure_finite(value: object) -> None:
    """Defensive helper for collector values that might be driver-specific floats."""

    if isinstance(value, float) and not math.isfinite(value):
        raise DatabaseSchemaError("non-finite metadata value is not portable")
