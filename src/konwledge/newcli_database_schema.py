"""Database-schema CLI commands with preflight validation and sanitized failures."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID

import typer
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from konwledge.application import KnowledgeLibrary
from konwledge.database_schema import (
    DatabaseCaptureResult,
    DatabaseConnectionError,
    DatabaseSchemaDiff,
    DatabaseSchemaError,
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
    PostgreSQLSchemaCollector,
    SchemaCollector,
    SQLiteSchemaCollector,
    dump_database_schema_json,
)
from konwledge.domain import Source, SourceKind
from konwledge.storage import (
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

DEFAULT_DATABASE = Path(".konwledge/knowledge.db")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_TITLE_LENGTH = 1_024
_MAX_AUTHOR_LENGTH = 1_024
_MAX_ENV_NAME_LENGTH = 128
_MAX_SCHEMA_LENGTH = 1_024
_SYSTEM_POSTGRES_SCHEMAS = frozenset({"information_schema", "pg_catalog"})

MetadataOption = Annotated[
    Path,
    typer.Option(
        "--database",
        "-d",
        help="Knowledge Library SQLite metadata file.",
        show_default=True,
        dir_okay=False,
        file_okay=True,
    ),
]


database_app = typer.Typer(
    name="database",
    help="Capture and inspect versioned database schemas without reading business rows.",
    no_args_is_help=True,
)


def _fail(message: str, *, code: int = 1) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


def _json_output(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _required_text(value: str, label: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        _fail(f"{label} must contain non-whitespace content", code=2)
    if len(normalized) > maximum:
        _fail(f"{label} must be at most {maximum} characters", code=2)
    return normalized


def _optional_text(value: str | None, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum=maximum)


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0 or value > 60:
        _fail("--timeout must be a finite number greater than 0 and at most 60", code=2)
    return value


def _validate_env_name(value: str) -> str:
    if len(value) > _MAX_ENV_NAME_LENGTH or _ENV_NAME.fullmatch(value) is None:
        _fail(
            "--dsn-env must be an environment name matching [A-Za-z_][A-Za-z0-9_]*",
            code=2,
        )
    return value


def _validate_schema(value: str) -> str:
    if (
        not value.strip()
        or len(value) > _MAX_SCHEMA_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(
            "--schema must be a non-empty PostgreSQL schema name without control characters", code=2
        )
    if value in _SYSTEM_POSTGRES_SCHEMAS or value.startswith("pg_"):
        _fail("system PostgreSQL schemas are excluded", code=2)
    return value


def _validate_postgres_dsn(value: str) -> None:
    try:
        parsed = make_url(value)
    except (ArgumentError, TypeError, ValueError):
        _fail("invalid PostgreSQL URL", code=2)
    if parsed.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        _fail("PostgreSQL URL must use postgres, postgresql, or postgresql+psycopg", code=2)


def _validate_metadata_path(database: Path) -> Path:
    candidate = database.expanduser()
    if candidate.exists() and not candidate.is_file():
        _fail("--database must be a regular file", code=2)
    return candidate.resolve(strict=False)


def _validate_sqlite_target(target: Path, database: Path) -> Path:
    candidate = target.expanduser()
    if not candidate.exists():
        _fail("SQLite target does not exist", code=2)
    if not candidate.is_file():
        _fail("SQLite target must be a regular file", code=2)
    try:
        resolved_target = candidate.resolve(strict=True)
    except OSError:
        _fail("SQLite target could not be resolved", code=2)
    metadata = _validate_metadata_path(database)
    same = resolved_target == metadata
    if database.expanduser().exists():
        with suppress(OSError):
            same = same or os.path.samefile(candidate, database.expanduser())
    if same:
        _fail("SQLite target must not be the Knowledge Library database", code=2)
    return resolved_target


@contextmanager
def _open_library(database: Path) -> Iterator[KnowledgeLibrary]:
    # Use the shared observer/exit adapter without exposing collector errors or
    # changing capture JSON. The delayed import avoids CLI registration cycles.
    from konwledge.cli import _open_scoped_library

    with _open_scoped_library(database) as library:
        yield library


class _CapturedCollector(SchemaCollector):
    """Return one already collected snapshot so persistence cannot recapture a moving catalog."""

    def __init__(self, snapshot: DatabaseSchemaSnapshot) -> None:
        self._snapshot = snapshot

    def capture(self) -> DatabaseSchemaSnapshot:
        return self._snapshot


def _sqlite_identity(path: Path) -> str:
    return f"sqlite\x00{path.as_posix()}"


def _postgres_identity(dsn: str, schema: str) -> str:
    try:
        parsed = make_url(dsn)
    except (TypeError, ValueError):
        _fail("invalid PostgreSQL URL", code=2)
    return json.dumps(
        [
            "postgresql",
            (parsed.host or "").casefold(),
            parsed.port or 5432,
            parsed.database or "",
            schema,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _capture_payload(result: DatabaseCaptureResult) -> dict[str, object]:
    return {
        "source_id": str(result.source_id),
        "document_id": str(result.document_id),
        "version": result.version,
        "content_hash": result.content_hash,
        "captured_at": result.captured_at.isoformat(),
        "created": result.created,
        "reverted": result.reverted,
        "schema_same": result.schema_same,
        "dialect": result.snapshot.dialect.value,
        "warnings": list(result.warnings),
        "diff": None if result.diff is None else result.diff.model_dump(mode="json"),
    }


def _print_capture(result: DatabaseCaptureResult, *, json_output: bool) -> None:
    payload = _capture_payload(result)
    if json_output:
        _json_output(payload)
        return
    action = "captured" if result.created else "unchanged"
    typer.echo(
        f"{action}: source={result.source_id} version={result.version} "
        f"dialect={result.snapshot.dialect.value} hash={result.content_hash}"
    )
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)


def _persist_capture(
    snapshot: DatabaseSchemaSnapshot,
    *,
    identity: str,
    title: str,
    author: str | None,
    database: Path,
) -> DatabaseCaptureResult:
    try:
        with _open_library(database) as library:
            return library.database_schema.capture_with(
                _CapturedCollector(snapshot),
                identity=identity,
                title=title,
                author=author,
            )
    except DatabaseSchemaError as error:
        _fail(str(error), code=2)
    except Exception:
        _fail("database schema metadata operation failed")


@database_app.command("capture-sqlite")
def capture_sqlite_command(
    target: Annotated[Path, typer.Argument(help="Existing SQLite database file to inspect.")],
    name: Annotated[str, typer.Option("--name", help="Non-secret source title.")],
    author: Annotated[
        str | None, typer.Option("--author", help="Optional audit attribution; not authentication.")
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Capture timeout in seconds (0 < value <= 60).")
    ] = 5.0,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output a stable capture result as JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Capture schema metadata from an existing SQLite file in read-only mode."""

    title = _required_text(name, "--name", maximum=_MAX_TITLE_LENGTH)
    audit_author = _optional_text(author, "--author", maximum=_MAX_AUTHOR_LENGTH)
    capture_timeout = _validate_timeout(timeout)
    resolved_target = _validate_sqlite_target(target, database)
    try:
        collector = SQLiteSchemaCollector(resolved_target, timeout_seconds=capture_timeout)
        snapshot = collector.capture()
    except DatabaseConnectionError:
        _fail("unable to open SQLite target read-only")
    except DatabaseSchemaError:
        _fail("SQLite schema capture failed")
    result = _persist_capture(
        snapshot,
        identity=_sqlite_identity(resolved_target),
        title=title,
        author=audit_author,
        database=database,
    )
    _print_capture(result, json_output=json_output)


@database_app.command("capture-postgres")
def capture_postgres_command(
    dsn_env: Annotated[
        str,
        typer.Option(
            "--dsn-env",
            help=(
                "Environment variable containing a PostgreSQL URL "
                "(postgresql, postgres, or postgresql+psycopg); the URL is never printed."
            ),
        ),
    ],
    name: Annotated[str, typer.Option("--name", help="Non-secret source title.")],
    schema: Annotated[
        str, typer.Option("--schema", help="One non-system PostgreSQL schema to inspect.")
    ] = "public",
    author: Annotated[
        str | None, typer.Option("--author", help="Optional audit attribution; not authentication.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output a stable capture result as JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Capture one PostgreSQL schema using a DSN read only from a named environment variable."""

    title = _required_text(name, "--name", maximum=_MAX_TITLE_LENGTH)
    audit_author = _optional_text(author, "--author", maximum=_MAX_AUTHOR_LENGTH)
    environment_name = _validate_env_name(dsn_env)
    selected_schema = _validate_schema(schema)
    _validate_metadata_path(database)

    dsn = os.environ.get(environment_name)
    if dsn is None or not dsn:
        _fail(f"environment variable {environment_name} is not set or is empty", code=2)
    _validate_postgres_dsn(dsn)
    try:
        collector = PostgreSQLSchemaCollector(dsn, schema=selected_schema)
    except DatabaseSchemaError:
        _fail("invalid PostgreSQL capture configuration", code=2)
    try:
        snapshot = collector.capture()
    except DatabaseConnectionError as error:
        message = str(error)
        if "optional 'postgres' extra" in message:
            _fail("PostgreSQL capture requires the optional 'postgres' extra (psycopg 3)")
        _fail("PostgreSQL schema capture failed")
    except DatabaseSchemaError:
        _fail("PostgreSQL schema capture failed")

    result = _persist_capture(
        snapshot,
        identity=_postgres_identity(dsn, selected_schema),
        title=title,
        author=audit_author,
        database=database,
    )
    _print_capture(result, json_output=json_output)


def _record_summary(record: DatabaseSchemaSnapshotRecord) -> dict[str, object]:
    tables = [table for namespace in record.snapshot.namespaces for table in namespace.tables]
    return {
        "source_id": str(record.source_id),
        "version": record.version,
        "document_id": str(record.document_id),
        "content_hash": record.content_hash,
        "captured_at": record.captured_at.isoformat(),
        "dialect": record.snapshot.dialect.value,
        "namespace_count": len(record.snapshot.namespaces),
        "table_count": len(tables),
        "column_count": sum(len(table.columns) for table in tables),
    }


def _list_database_sources(
    database: Path,
) -> tuple[tuple[Source, Sequence[DatabaseSchemaSnapshotRecord]], ...]:
    database_path = database.expanduser().resolve(strict=False)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        create_schema(engine)
        with UnitOfWork(create_session_factory(engine)) as uow:
            sources = sorted(
                (source for source in uow.sources.list() if source.kind is SourceKind.DATABASE),
                key=lambda source: str(source.id),
            )
            return tuple(
                (source, tuple(uow.database_schema_snapshots.list_by_source(source.id)))
                for source in sources
            )
    finally:
        engine.dispose()


def _read_error(error: Exception) -> NoReturn:
    if isinstance(error, DatabaseSchemaError):
        _fail(str(error), code=2)
    _fail("database schema metadata read failed")


@database_app.command("list")
def database_list_command(
    json_output: Annotated[
        bool, typer.Option("--json", help="Output database sources as stable JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """List captured database sources and their latest immutable versions."""

    try:
        entries = _list_database_sources(database)
    except Exception as error:
        _read_error(error)
    payload = [
        {
            "source_id": str(source.id),
            "title": source.title,
            "dialect": source.metadata.get("dialect"),
            "versions": len(records),
            "latest_version": None if not records else records[-1].version,
            "latest_content_hash": None if not records else records[-1].content_hash,
            "latest_captured_at": None if not records else records[-1].captured_at.isoformat(),
        }
        for source, records in entries
    ]
    if json_output:
        _json_output(payload)
        return
    if not payload:
        typer.echo("No database schema captures found.")
        return
    for item in payload:
        typer.echo(
            f"{item['source_id']} | {item['dialect']} | versions={item['versions']} "
            f"latest={item['latest_version']} | {item['title']}"
        )


@database_app.command("history")
def database_history_command(
    source_id: Annotated[UUID, typer.Argument(help="Database source UUID.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Output immutable capture history as stable JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Show every stored version for one database source."""

    try:
        with _open_library(database) as library:
            records = library.database_schema.history(source_id)
    except Exception as error:
        _read_error(error)
    payload = [_record_summary(record) for record in records]
    if json_output:
        _json_output(payload)
        return
    typer.echo(f"source: {source_id}")
    typer.echo(f"versions: {len(payload)}")
    for item in payload:
        typer.echo(
            f"  version={item['version']} dialect={item['dialect']} "
            f"tables={item['table_count']} columns={item['column_count']} "
            f"hash={item['content_hash']} captured_at={item['captured_at']}"
        )


def _get_snapshot(source_id: UUID, version: int, database: Path) -> DatabaseSchemaSnapshotRecord:
    try:
        with _open_library(database) as library:
            return library.database_schema.get_snapshot(source_id, version)
    except Exception as error:
        _read_error(error)


@database_app.command("show")
def database_show_command(
    source_id: Annotated[UUID, typer.Argument(help="Database source UUID.")],
    version: Annotated[int, typer.Option("--version", min=1, help="Exact capture version.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the typed snapshot record as stable JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Show one immutable database schema snapshot version."""

    record = _get_snapshot(source_id, version, database)
    if json_output:
        _json_output(record.model_dump(mode="json"))
        return
    summary = _record_summary(record)
    typer.echo(
        f"source: {source_id}\nversion: {version}\ndocument: {record.document_id}\n"
        f"dialect: {record.snapshot.dialect.value}\nhash: {record.content_hash}\n"
        f"captured_at: {record.captured_at.isoformat()}\n"
        f"namespaces: {summary['namespace_count']}\ntables: {summary['table_count']}\n"
        f"columns: {summary['column_count']}"
    )


def _write_export(path: Path, content: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        _fail("output already exists; pass --overwrite to replace it", code=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if overwrite else "x"
        with path.open(mode, encoding="utf-8", newline="") as stream:
            stream.write(content)
    except FileExistsError:
        _fail("output already exists; pass --overwrite to replace it", code=2)
    except (OSError, UnicodeError):
        _fail("could not write database schema output")


@database_app.command("export")
def database_export_command(
    source_id: Annotated[UUID, typer.Argument(help="Database source UUID.")],
    version: Annotated[int, typer.Option("--version", min=1, help="Exact capture version.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write JSON to a file instead of stdout.")
    ] = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Explicitly replace an existing output file.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Export one portable schema-only snapshot as deterministic JSON."""

    if output is not None and output.exists() and not overwrite:
        _fail("output already exists; pass --overwrite to replace it", code=2)
    record = _get_snapshot(source_id, version, database)
    content = dump_database_schema_json(record.snapshot)
    if output is None:
        typer.echo(content, nl=False)
        return
    _write_export(output, content, overwrite=overwrite)
    typer.echo(f"exported: {output}")


def _diff_payload(
    source_id: UUID, from_version: int, to_version: int, diff: DatabaseSchemaDiff
) -> dict[str, object]:
    return {
        "source_id": str(source_id),
        "from_version": from_version,
        "to_version": to_version,
        "is_noop": diff.is_noop,
        "added_tables": list(diff.added_tables),
        "removed_tables": list(diff.removed_tables),
        "changed_tables": [item.model_dump(mode="json") for item in diff.changed_tables],
    }


@database_app.command("diff")
def database_diff_command(
    source_id: Annotated[UUID, typer.Argument(help="Database source UUID.")],
    from_version: Annotated[
        int, typer.Option("--from-version", min=1, help="Earlier capture version.")
    ],
    to_version: Annotated[int, typer.Option("--to-version", min=1, help="Later capture version.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the typed schema diff as stable JSON.")
    ] = False,
    database: MetadataOption = DEFAULT_DATABASE,
) -> None:
    """Diff two stored versions without reconnecting to the source database."""

    if from_version == to_version:
        _fail("--from-version and --to-version must be different", code=2)
    try:
        with _open_library(database) as library:
            diff = library.database_schema.diff(source_id, from_version, to_version)
    except Exception as error:
        _read_error(error)
    payload = _diff_payload(source_id, from_version, to_version, diff)
    if json_output:
        _json_output(payload)
        return
    typer.echo(
        f"source: {source_id}\nversions: {from_version} -> {to_version}\n"
        f"noop: {str(diff.is_noop).lower()}"
    )
    for table in diff.added_tables:
        typer.echo(f"  added table: {table}")
    for table in diff.removed_tables:
        typer.echo(f"  removed table: {table}")
    for change in diff.changed_tables:
        typer.echo(
            f"  changed table: {change.namespace}.{change.name} "
            f"added_columns={','.join(change.added_columns) or '-'} "
            f"removed_columns={','.join(change.removed_columns) or '-'}"
        )
