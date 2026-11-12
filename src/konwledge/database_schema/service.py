"""Atomic database schema capture and knowledge-library publication."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.engine import make_url

from konwledge.database_schema.collector import (
    PostgreSQLSchemaCollector,
    SchemaCollector,
    SQLiteSchemaCollector,
)
from konwledge.database_schema.models import (
    DatabaseCaptureResult,
    DatabaseSchemaDiff,
    DatabaseSchemaError,
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
    DatabaseTable,
    diff_database_schemas,
)
from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.lifecycle import (
    diff_by_lineage,
    initial_revisions,
    load_timeline,
    materialize_statuses,
    publish_document,
    revisions_for_diff,
)
from konwledge.lifecycle.status import head_at
from konwledge.storage import SessionFactory, UnitOfWork

IdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]
_MATERIALIZATION_NAMESPACE = uuid5(NAMESPACE_URL, "konwledge:database-schema:v1")


class DatabaseSchemaService:
    """Read an external catalog first, then atomically store and publish the result."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: IdFactory = uuid4,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def capture_sqlite(
        self,
        path: str | Path,
        *,
        title: str,
        author: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> DatabaseCaptureResult:
        """Capture an existing SQLite regular file without creating or attaching databases."""

        display_title = _nonblank(title, "title")
        target = Path(path).expanduser()
        self._reject_metadata_database(target)
        collector = SQLiteSchemaCollector(target, timeout_seconds=timeout_seconds)
        # External read completes before opening a local metadata transaction.
        snapshot = collector.capture()
        identity = _sqlite_identity(collector.path)
        return self._store(
            snapshot,
            identity=identity,
            title=display_title,
            author=author,
        )

    def capture_postgresql(
        self,
        dsn: str,
        *,
        title: str,
        schema: str = "public",
        author: str | None = None,
    ) -> DatabaseCaptureResult:
        """Capture one PostgreSQL schema.

        The capture uses psycopg 3 in a read-only repeatable-read transaction.
        """

        display_title = _nonblank(title, "title")
        collector = PostgreSQLSchemaCollector(dsn, schema=schema)
        # The URL, credentials, username, and query parameters never enter local storage.
        snapshot = collector.capture()
        identity = _postgresql_identity(dsn, schema=collector.schema)
        return self._store(
            snapshot,
            identity=identity,
            title=display_title,
            author=author,
        )

    def capture_with(
        self,
        collector: SchemaCollector,
        *,
        identity: str,
        title: str,
        author: str | None = None,
    ) -> DatabaseCaptureResult:
        """Capture through an injected collector (primarily useful to test atomic persistence)."""

        display_title = _nonblank(title, "title")
        canonical_identity = _nonblank(identity, "identity")
        snapshot = collector.capture()
        return self._store(
            snapshot,
            identity=canonical_identity,
            title=display_title,
            author=author,
        )

    def history(self, source_id: UUID) -> tuple[DatabaseSchemaSnapshotRecord, ...]:
        """Return append-only captures for one source in ordinal version order."""

        with UnitOfWork(self._session_factory) as uow:
            source = uow.sources.get(source_id)
            if source is None or source.kind is not SourceKind.DATABASE:
                raise DatabaseSchemaError("database source was not found")
            return tuple(uow.database_schema_snapshots.list_by_source(source_id))

    def get_snapshot(self, source_id: UUID, version: int) -> DatabaseSchemaSnapshotRecord:
        """Return one immutable capture version."""

        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DatabaseSchemaError("version must be a positive integer")
        with UnitOfWork(self._session_factory) as uow:
            record = uow.database_schema_snapshots.get(source_id, version)
            if record is None:
                raise DatabaseSchemaError("database schema snapshot was not found")
            return record

    def diff(self, source_id: UUID, from_version: int, to_version: int) -> DatabaseSchemaDiff:
        """Diff two stored versions without connecting to the external database."""

        before = self.get_snapshot(source_id, from_version)
        after = self.get_snapshot(source_id, to_version)
        return diff_database_schemas(before.snapshot, after.snapshot)

    def _store(
        self,
        snapshot: DatabaseSchemaSnapshot,
        *,
        identity: str,
        title: str,
        author: str | None,
    ) -> DatabaseCaptureResult:
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        source_uri = f"database://{snapshot.dialect.value}/{identity_hash}"
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DatabaseSchemaError("clock must return a timezone-aware timestamp")

        with UnitOfWork(self._session_factory) as uow:
            source = uow.sources.get_by_kind_uri(kind=SourceKind.DATABASE, uri=source_uri)
            if source is None:
                source = Source(
                    id=self._id_factory(),
                    kind=SourceKind.DATABASE,
                    uri=source_uri,
                    title=title,
                    created_at=now,
                    metadata={"dialect": snapshot.dialect.value},
                )
                uow.sources.add(source)

            timeline = load_timeline(uow, source.id)
            head = head_at(timeline)
            head_record = (
                None
                if head is None
                else uow.database_schema_snapshots.get_by_document(head.document_id)
            )
            if head_record is not None and head_record.content_hash == snapshot.content_hash:
                return DatabaseCaptureResult(
                    source_id=source.id,
                    document_id=head_record.document_id,
                    version=head_record.version,
                    content_hash=head_record.content_hash,
                    captured_at=head_record.captured_at,
                    created=False,
                    reverted=False,
                    schema_same=True,
                    snapshot=head_record.snapshot,
                    diff=None,
                )

            rendered, sections = render_database_schema(snapshot)
            document_checksum = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            existing_document = uow.documents.get_by_import_identity(
                source_id=source.id,
                locator=source_uri,
                checksum=document_checksum,
            )
            document_created = existing_document is None
            if existing_document is None:
                document = Document(
                    id=_stable_uuid(source.id, document_checksum, "document"),
                    source_id=source.id,
                    locator=source_uri,
                    content_type=DocumentContentType.TEXT,
                    checksum=document_checksum,
                    ingested_at=now,
                    metadata={
                        "database_schema_version": snapshot.schema_version,
                        "schema_content_hash": snapshot.content_hash,
                    },
                )
                uow.documents.add(document)
                units = _materialize_units(source.id, document, rendered, sections)
                for unit in units:
                    uow.knowledge_units.add(unit)
            else:
                document = existing_document
                units = tuple(uow.knowledge_units.list_by_document(document.id))
                if not units:
                    raise DatabaseSchemaError("stored database document is missing schema units")

            previous_document_id = None if head is None else head.document_id
            previous_units: Sequence[KnowledgeUnit] = (
                ()
                if previous_document_id is None
                else uow.knowledge_units.list_by_document(previous_document_id)
            )
            if previous_document_id is None:
                revisions = initial_revisions(
                    list(units),
                    document=document,
                    id_factory=self._id_factory,
                    clock=lambda: now,
                    next_number=_revision_counter(uow),
                )
                schema_diff = None
            else:
                unit_diff = diff_by_lineage(previous_units, units)
                revisions = revisions_for_diff(
                    unit_diff,
                    document=document,
                    previous_document_id=previous_document_id,
                    id_factory=self._id_factory,
                    clock=lambda: now,
                    next_number=_revision_counter(uow),
                    extra_payload={
                        "database_schema": True,
                        "reverted": not document_created,
                    },
                )
                schema_diff = (
                    None
                    if head_record is None
                    else diff_database_schemas(head_record.snapshot, snapshot)
                )

            for revision in revisions:
                if author is not None:
                    revision = revision.model_copy(update={"author": author})
                uow.revisions.add(revision)

            version = uow.database_schema_snapshots.next_version(source.id)
            record = DatabaseSchemaSnapshotRecord(
                source_id=source.id,
                version=version,
                snapshot=snapshot,
                content_hash=snapshot.content_hash,
                captured_at=now,
                document_id=document.id,
            )
            uow.database_schema_snapshots.add(record)
            publish_document(
                uow,
                document,
                previous_document_id=previous_document_id,
                reverted=not document_created,
                id_factory=self._id_factory,
                created_at=now,
            )
            materialize_statuses(uow, source.id)
            uow.commit()

            return DatabaseCaptureResult(
                source_id=source.id,
                document_id=document.id,
                version=version,
                content_hash=snapshot.content_hash,
                captured_at=now,
                created=True,
                reverted=not document_created,
                schema_same=False,
                snapshot=snapshot,
                diff=schema_diff,
            )

    def _reject_metadata_database(self, target: Path) -> None:
        """Reject the Knowledge Library SQLite file, including hard-link aliases."""

        bind = self._session_factory.kw.get("bind")
        if bind is None or bind.dialect.name != "sqlite":
            return
        database = bind.url.database
        if not database or database == ":memory:":
            return
        metadata_path = Path(database).expanduser()
        if not target.exists() or not metadata_path.exists():
            return
        try:
            same = os.path.samefile(target, metadata_path)
        except OSError:
            same = target.resolve() == metadata_path.resolve()
        if same:
            raise DatabaseSchemaError("SQLite target must not be the Knowledge Library database")


def render_database_schema(
    snapshot: DatabaseSchemaSnapshot,
) -> tuple[str, tuple[tuple[DatabaseTable | None, int, int], ...]]:
    """Render schema-only text with exact offsets and JSON-escaped external metadata."""

    parts: list[str] = []
    sections: list[tuple[DatabaseTable | None, int, int]] = []
    tables = [table for namespace in snapshot.namespaces for table in namespace.tables]
    if not tables:
        text = (
            "Database schema\n"
            f"dialect: {json.dumps(snapshot.dialect.value, ensure_ascii=False)}\n"
            "objects: 0\n"
        )
        return text, ((None, 0, len(text)),)

    for table in tables:
        start = sum(len(part) for part in parts)
        lines = [
            "Database object\n",
            f"key: {table.key}\n",
            f"type: {json.dumps(table.object_type.value, ensure_ascii=False)}\n",
            f"comment: {_json(table.comment)}\n",
            f"without_rowid: {_json(table.without_rowid)}\n",
            f"strict: {_json(table.strict)}\n",
            f"view_definition: {_json(table.view_definition)}\n",
        ]
        lines.extend(
            f"column: {_json(column.model_dump(mode='json'))}\n" for column in table.columns
        )
        lines.extend(f"index: {_json(index.model_dump(mode='json'))}\n" for index in table.indexes)
        lines.extend(
            f"foreign_key: {_json(foreign_key.model_dump(mode='json'))}\n"
            for foreign_key in table.foreign_keys
        )
        lines.append("\n")
        section = "".join(lines)
        parts.append(section)
        sections.append((table, start, start + len(section)))
    return "".join(parts), tuple(sections)


def _materialize_units(
    source_id: UUID,
    document: Document,
    rendered: str,
    sections: Sequence[tuple[DatabaseTable | None, int, int]],
) -> tuple[KnowledgeUnit, ...]:
    units: list[KnowledgeUnit] = []
    for ordinal, (table, start, end) in enumerate(sections):
        key = "__empty__" if table is None else table.key
        units.append(
            KnowledgeUnit(
                id=_stable_uuid(document.id, key, "unit"),
                lineage_id=_stable_uuid(source_id, key, "lineage"),
                document_id=document.id,
                ordinal=ordinal,
                text=rendered[start:end],
                start_char=start,
                end_char=end,
                heading=None if table is None else table.name,
                section_path=() if table is None else (table.namespace, table.name),
                tags={"database-schema"},
                metadata={
                    "database_schema": True,
                    "table_key": key,
                    "object_type": None if table is None else table.object_type.value,
                    "column_names": []
                    if table is None
                    else [column.name for column in table.columns],
                    "columns": []
                    if table is None
                    else [column.model_dump(mode="json") for column in table.columns],
                },
            )
        )
    return tuple(units)


def _revision_counter(uow: UnitOfWork) -> Callable[[UUID], int]:
    issued: dict[UUID, int] = {}

    def next_number(lineage_id: UUID) -> int:
        if lineage_id not in issued:
            issued[lineage_id] = uow.revisions.next_number(
                RevisionEntityType.KNOWLEDGE_UNIT, lineage_id
            )
        else:
            issued[lineage_id] += 1
        return issued[lineage_id]

    return next_number


def _stable_uuid(first: UUID, value: str, entity: str) -> UUID:
    return uuid5(_MATERIALIZATION_NAMESPACE, f"{first}:{entity}:{value}")


def _sqlite_identity(path: Path) -> str:
    return f"sqlite\x00{path.as_posix()}"


def _postgresql_identity(dsn: str, *, schema: str) -> str:
    try:
        parsed = make_url(dsn)
    except (TypeError, ValueError) as exc:  # collector normally rejects first
        raise DatabaseSchemaError("invalid PostgreSQL URL") from exc
    host = (parsed.host or "").casefold()
    port = parsed.port or 5432
    database = parsed.database or ""
    return json.dumps(
        ["postgresql", host, port, database, schema],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatabaseSchemaError(f"{name} must contain non-whitespace content")
    return value.strip()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
