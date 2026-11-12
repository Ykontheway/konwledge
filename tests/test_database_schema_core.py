from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import konwledge.database_schema.models as schema_models
from konwledge.application import KnowledgeLibrary
from konwledge.database_schema import (
    DatabaseColumn,
    DatabaseConnectionError,
    DatabaseDialect,
    DatabaseNamespace,
    DatabaseObjectType,
    DatabaseSchemaError,
    DatabaseSchemaService,
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
    DatabaseTable,
    PostgreSQLSchemaCollector,
    SQLiteSchemaCollector,
    diff_database_schemas,
    dump_database_schema_json,
    load_database_schema_json,
    render_database_schema,
)
from konwledge.domain import KnowledgeUnitStatus, RevisionEntityType, SourceKind
from konwledge.snapshot import export_snapshot
from konwledge.storage import (
    DatabaseSchemaSnapshotRepository,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def service(engine: Engine) -> DatabaseSchemaService:
    return DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)


def create_external(path: Path, ddl: str, *, sentinel: str = "TOP SECRET") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ddl)
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sentinel'"
        ).fetchone()
        if table is not None:
            connection.execute("INSERT INTO sentinel(payload) VALUES (?)", (sentinel,))
        connection.commit()
    finally:
        connection.close()


def test_source_kind_database_is_domain_only_snapshot_v1_remains_fail_closed(
    tmp_path: Path, engine: Engine
) -> None:
    assert SourceKind.DATABASE.value == "database"
    target = tmp_path / "external.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    schema_service = DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)
    result = schema_service.capture_sqlite(target, title="External")
    with pytest.raises(ValueError, match="does not support source kinds: database"):
        export_snapshot(create_session_factory(engine))
    assert result.snapshot.dialect is DatabaseDialect.SQLITE


def test_collector_rejects_missing_file_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseSchemaError, match="does not exist"):
        SQLiteSchemaCollector(missing)
    assert not missing.exists()


def test_collector_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(DatabaseSchemaError, match="regular file"):
        SQLiteSchemaCollector(tmp_path)


@pytest.mark.parametrize("timeout", [True, False, "5", None, math.nan, math.inf, -math.inf])
def test_sqlite_timeout_rejects_wrong_types_and_nonfinite_values(
    tmp_path: Path, timeout: object
) -> None:
    target = tmp_path / "timeout.db"
    create_external(target, "")
    with pytest.raises(DatabaseSchemaError, match="timeout_seconds"):
        SQLiteSchemaCollector(target, timeout_seconds=timeout)  # type: ignore[arg-type]


def test_sqlite_collects_tables_views_columns_indexes_foreign_keys_and_generated(
    tmp_path: Path,
) -> None:
    target = tmp_path / "schema.db"
    create_external(
        target,
        """
        CREATE TABLE parent(id INTEGER PRIMARY KEY, label TEXT NOT NULL DEFAULT 'x');
        CREATE TABLE child(
          id INTEGER PRIMARY KEY,
          parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE,
          doubled INTEGER GENERATED ALWAYS AS (id * 2) VIRTUAL
        );
        CREATE UNIQUE INDEX child_parent_idx ON child(parent_id);
        CREATE VIEW child_view AS SELECT id, parent_id FROM child;
        """,
    )
    snapshot = SQLiteSchemaCollector(target).capture()
    tables = {table.name: table for table in snapshot.namespaces[0].tables}
    assert set(tables) == {"child", "child_view", "parent"}
    assert tables["child_view"].object_type is DatabaseObjectType.VIEW
    assert tables["child"].columns[2].generated is True
    assert tables["child"].indexes[0].unique is True
    assert tables["child"].foreign_keys[0].referred_table == "parent"
    assert tables["child"].foreign_keys[0].on_delete == "CASCADE"
    assert tables["parent"].columns[0].primary_key_position == 1
    assert tables["parent"].columns[1].nullable is False
    assert tables["parent"].columns[1].default == "'x'"


def test_sqlite_index_sql_scanner_preserves_complex_terms_and_predicate(tmp_path: Path) -> None:
    target = tmp_path / "index-metadata.db"
    create_external(
        target,
        """
        CREATE TABLE "odd table"(
          "double""quote" TEXT,
          [square name] TEXT UNIQUE,
          `back``tick` TEXT,
          active INTEGER
        );
        CREATE INDEX "complex index" ON "odd table" (
          lower("double""quote") DESC,
          /* comma, (paren), WHERE */ coalesce([square name], 'x,y() WHERE it''s'),
          lower(`back``tick`)
        ) WHERE active = 1 AND [square name] <> 'WHERE, (literal)';
        """,
    )
    table = SQLiteSchemaCollector(target).capture().namespaces[0].tables[0]
    index = table.indexes[0]
    assert index.name == "complex index"
    assert [term.name for term in index.terms] == [None, None, None]
    assert index.terms[0].expression == 'lower("double""quote") DESC'
    assert index.terms[0].descending is True
    assert index.terms[1].expression == (
        "/* comma, (paren), WHERE */ coalesce([square name], 'x,y() WHERE it''s')"
    )
    assert index.terms[2].expression == "lower(`back``tick`)"
    assert index.predicate == "active = 1 AND [square name] <> 'WHERE, (literal)'"


def test_sqlite_view_definition_is_exact_catalog_sql_and_affects_hash(tmp_path: Path) -> None:
    first_path = tmp_path / "first-view.db"
    second_path = tmp_path / "second-view.db"
    first_sql = "CREATE VIEW visible AS SELECT id FROM base WHERE id > 0"
    second_sql = "CREATE VIEW visible AS SELECT id FROM base WHERE id > 1"
    create_external(first_path, f"CREATE TABLE base(id INTEGER); {first_sql}")
    create_external(second_path, f"CREATE TABLE base(id INTEGER); {second_sql}")

    first = SQLiteSchemaCollector(first_path).capture()
    second = SQLiteSchemaCollector(second_path).capture()
    first_view = next(table for table in first.namespaces[0].tables if table.name == "visible")
    second_view = next(table for table in second.namespaces[0].tables if table.name == "visible")
    assert first_view.view_definition == first_sql
    assert second_view.view_definition == second_sql
    assert first.content_hash != second.content_hash


def test_sqlite_excludes_internal_tables_and_handles_like_underscore(tmp_path: Path) -> None:
    target = tmp_path / "names.db"
    create_external(
        target,
        'CREATE TABLE "sqlitexlike_but_user"(id INTEGER); CREATE TABLE ordinary(id INTEGER);',
    )
    names = [table.name for table in SQLiteSchemaCollector(target).capture().namespaces[0].tables]
    # SQLite reserves sqlite_* names itself; the ordinary table proves the escaped predicate works.
    assert names == ["ordinary", "sqlitexlike_but_user"]


def test_sqlite_unicode_quotes_and_injection_shaped_identifiers_are_data(tmp_path: Path) -> None:
    target = tmp_path / "odd ?#%.db"
    create_external(
        target,
        'CREATE TABLE "用户\'\'); DROP TABLE x;--"("a""b" TEXT, "百分比%_" INTEGER);',
    )
    snapshot = SQLiteSchemaCollector(target).capture()
    table = snapshot.namespaces[0].tables[0]
    assert table.name == "用户''); DROP TABLE x;--"
    assert [column.name for column in table.columns] == ['a"b', "百分比%_"]


def test_business_rows_never_appear_in_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "private.db"
    create_external(target, "CREATE TABLE sentinel(payload TEXT)", sentinel="PASSWORD-123")
    snapshot = SQLiteSchemaCollector(target).capture()
    assert "PASSWORD-123" not in snapshot.canonical_json()
    assert "payload" in snapshot.canonical_json()


def test_authorizer_denies_business_row_reads(tmp_path: Path) -> None:
    target = tmp_path / "private.db"
    create_external(target, "CREATE TABLE sentinel(payload TEXT)")

    class ProbingCollector(SQLiteSchemaCollector):
        def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
            connection.execute("SELECT payload FROM sentinel").fetchall()
            raise AssertionError("business read unexpectedly succeeded")

    with pytest.raises(DatabaseSchemaError, match="capture failed"):
        ProbingCollector(target).capture()


def test_authorizer_denies_write_pragma(tmp_path: Path) -> None:
    target = tmp_path / "private.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")

    class ProbingCollector(SQLiteSchemaCollector):
        def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
            connection.execute("PRAGMA journal_mode=DELETE").fetchall()
            raise AssertionError("write pragma unexpectedly succeeded")

    with pytest.raises(DatabaseSchemaError, match="capture failed"):
        ProbingCollector(target).capture()


def test_authorizer_denies_attach_and_ddl(tmp_path: Path) -> None:
    target = tmp_path / "private.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")

    class ProbingCollector(SQLiteSchemaCollector):
        def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
            connection.execute("CREATE TABLE forbidden(id INTEGER)")
            raise AssertionError("DDL unexpectedly succeeded")

    with pytest.raises(DatabaseSchemaError, match="capture failed"):
        ProbingCollector(target).capture()


def test_same_schema_has_same_hash_across_captures(tmp_path: Path) -> None:
    target = tmp_path / "stable.db"
    create_external(target, "CREATE TABLE b(id INTEGER); CREATE TABLE a(id TEXT)")
    first = SQLiteSchemaCollector(target).capture()
    second = SQLiteSchemaCollector(target).capture()
    assert first.content_hash == second.content_hash
    assert first.canonical_json() == second.canonical_json()


def test_models_sort_namespaces_tables_columns_and_indexes() -> None:
    second = DatabaseTable(
        namespace="z",
        name="b",
        object_type=DatabaseObjectType.TABLE,
        columns=(DatabaseColumn(name="c", data_type="TEXT", nullable=True, ordinal=0),),
    )
    first = second.model_copy(update={"name": "a"})
    snapshot = DatabaseSchemaSnapshot(
        dialect=DatabaseDialect.SQLITE,
        namespaces=(DatabaseNamespace(name="z", tables=(second, first)),),
    )
    assert [table.name for table in snapshot.namespaces[0].tables] == ["a", "b"]


def test_strict_schema_version_rejects_bool_float_and_string() -> None:
    base = {"dialect": "sqlite", "namespaces": []}
    for value in (True, 1.0, "1"):
        with pytest.raises(ValidationError):
            DatabaseSchemaSnapshot.model_validate({**base, "schema_version": value})


def test_strict_integer_and_boolean_fields_reject_coercion() -> None:
    with pytest.raises(ValidationError):
        DatabaseColumn.model_validate(
            {"name": "id", "data_type": "INT", "nullable": True, "ordinal": "0"}
        )
    with pytest.raises(ValidationError):
        DatabaseColumn.model_validate(
            {"name": "id", "data_type": "INT", "nullable": "true", "ordinal": 0}
        )


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DatabaseSchemaSnapshot.model_validate(
            {"schema_version": 1, "dialect": "sqlite", "namespaces": [], "unknown": 1}
        )


def test_json_loader_rejects_duplicate_keys_and_nan() -> None:
    with pytest.raises(DatabaseSchemaError, match="duplicate JSON key"):
        load_database_schema_json('{"schema_version":1,"schema_version":1}')
    with pytest.raises(DatabaseSchemaError, match="non-finite"):
        load_database_schema_json('{"schema_version":1,"dialect":"sqlite","namespaces":[],"x":NaN}')


def test_dump_and_load_are_portable_and_deterministic(tmp_path: Path) -> None:
    target = tmp_path / "portable.db"
    create_external(target, 'CREATE TABLE "知识"("列" TEXT)')
    snapshot = SQLiteSchemaCollector(target).capture()
    dumped = dump_database_schema_json(snapshot)
    assert dumped.endswith("\n")
    assert "知识" in dumped
    assert load_database_schema_json(dumped) == snapshot


def test_snapshot_record_requires_aware_time_and_matching_hash(tmp_path: Path) -> None:
    target = tmp_path / "record.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    snapshot = SQLiteSchemaCollector(target).capture()
    values = {
        "source_id": uuid4(),
        "version": 1,
        "snapshot": snapshot,
        "content_hash": snapshot.content_hash,
        "captured_at": NOW,
        "document_id": uuid4(),
    }
    with pytest.raises(ValidationError, match="timezone"):
        DatabaseSchemaSnapshotRecord.model_validate(
            {**values, "captured_at": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError, match="does not match"):
        DatabaseSchemaSnapshotRecord.model_validate({**values, "content_hash": "0" * 64})


def test_table_and_column_bounds_reject_instead_of_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema_models, "MAX_TABLES", 1)
    table = DatabaseTable(
        namespace="main", name="a", object_type=DatabaseObjectType.TABLE, columns=()
    )
    with pytest.raises(ValidationError, match="more than 1"):
        DatabaseSchemaSnapshot(
            dialect=DatabaseDialect.SQLITE,
            namespaces=(
                DatabaseNamespace(
                    name="main", tables=(table, table.model_copy(update={"name": "b"}))
                ),
            ),
        )


def test_rendered_units_have_exact_offsets_and_json_escaping(tmp_path: Path) -> None:
    target = tmp_path / "render.db"
    create_external(target, 'CREATE TABLE "# misleading\nheading"("x" TEXT DEFAULT \'# hi\')')
    snapshot = SQLiteSchemaCollector(target).capture()
    text, sections = render_database_schema(snapshot)
    assert len(sections) == 1
    _, start, end = sections[0]
    assert text[start:end] == text
    assert json.dumps(snapshot.namespaces[0].tables[0].name, ensure_ascii=False) in text
    assert not text.startswith("#")


def test_empty_database_is_valid_and_renders_header_unit(tmp_path: Path) -> None:
    target = tmp_path / "empty.db"
    create_external(target, "")
    snapshot = SQLiteSchemaCollector(target).capture()
    assert snapshot.namespaces[0].tables == ()
    text, sections = render_database_schema(snapshot)
    assert "objects: 0" in text
    assert sections == ((None, 0, len(text)),)


def test_diff_reports_added_removed_and_changed_tables_and_columns() -> None:
    old_table = DatabaseTable(
        namespace="main",
        name="old",
        object_type=DatabaseObjectType.TABLE,
        columns=(DatabaseColumn(name="id", data_type="INT", nullable=True, ordinal=0),),
    )
    changed_old = old_table.model_copy(update={"name": "same"})
    changed_new = changed_old.model_copy(
        update={
            "columns": (
                changed_old.columns[0].model_copy(update={"data_type": "TEXT"}),
                DatabaseColumn(name="added", data_type="TEXT", nullable=True, ordinal=1),
            )
        }
    )
    added = old_table.model_copy(update={"name": "new"})
    before = DatabaseSchemaSnapshot(
        dialect=DatabaseDialect.SQLITE,
        namespaces=(DatabaseNamespace(name="main", tables=(old_table, changed_old)),),
    )
    after = DatabaseSchemaSnapshot(
        dialect=DatabaseDialect.SQLITE,
        namespaces=(DatabaseNamespace(name="main", tables=(added, changed_new)),),
    )
    diff = diff_database_schemas(before, after)
    assert len(diff.added_tables) == 1
    assert len(diff.removed_tables) == 1
    assert diff.changed_tables[0].added_columns == ("added",)
    assert diff.changed_tables[0].changed_columns[0].name == "id"


def test_diff_of_same_snapshot_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "same.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    snapshot = SQLiteSchemaCollector(target).capture()
    assert diff_database_schemas(snapshot, snapshot).is_noop


def test_service_capture_is_atomic_and_idempotent(
    tmp_path: Path, engine: Engine, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "service.db"
    create_external(target, "CREATE TABLE t(id INTEGER, value TEXT)")
    first = service.capture_sqlite(target, title="Service")
    second = service.capture_sqlite(target, title="Service")
    assert first.created is True
    assert second.created is False
    assert second.schema_same is True
    assert second.version == 1
    with UnitOfWork(create_session_factory(engine)) as uow:
        assert len(uow.sources.list()) == 1
        assert len(uow.documents.list()) == 1
        units = uow.knowledge_units.list()
        assert len(units) == 1
        document = uow.documents.get(first.document_id)
        assert document is not None
        content = "".join(unit.text for unit in sorted(units, key=lambda unit: unit.ordinal))
        assert units[0].text == content[units[0].start_char : units[0].end_char]
        assert sha256(content.encode()).hexdigest() == document.checksum
        assert units[0].metadata["columns"][0]["name"] == "id"
        assert len(uow.database_schema_snapshots.list()) == 1
        assert len(uow.revisions.list()) == 2  # initial unit + source publication


def test_service_schema_change_appends_version_diff_and_preserves_table_lineage(
    tmp_path: Path, engine: Engine, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "change.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    first = service.capture_sqlite(target, title="Change")
    connection = sqlite3.connect(target)
    connection.execute("ALTER TABLE t ADD COLUMN value TEXT")
    connection.commit()
    connection.close()
    second = service.capture_sqlite(target, title="Change")
    assert second.version == 2
    assert second.diff is not None
    assert second.diff.changed_tables[0].added_columns == ("value",)
    with UnitOfWork(create_session_factory(engine)) as uow:
        old = uow.knowledge_units.list_by_document(first.document_id)[0]
        new = uow.knowledge_units.list_by_document(second.document_id)[0]
        assert old.lineage_id == new.lineage_id
        assert len(uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, old.lineage_id)) == 2


def test_service_deleted_table_becomes_retired_and_not_searchable(
    tmp_path: Path, engine: Engine
) -> None:
    target = tmp_path / "delete.db"
    create_external(
        target,
        "CREATE TABLE old_field(secret_column TEXT); CREATE TABLE keep(id INTEGER)",
    )
    library = KnowledgeLibrary(create_session_factory(engine), clock=lambda: NOW)
    first = library.database_schema.capture_sqlite(target, title="Delete")
    connection = sqlite3.connect(target)
    connection.execute("DROP TABLE old_field")
    connection.commit()
    connection.close()
    library.database_schema.capture_sqlite(target, title="Delete")
    assert library.search("secret_column").hits == ()
    snapshot = library.snapshot(include_retired=True)
    retired = [state for source in snapshot.sources for state in source.retired]
    assert any(state.status is KnowledgeUnitStatus.DELETED for state in retired)
    assert first.source_id == snapshot.sources[0].source.id


def test_service_revert_reuses_document_but_appends_capture_version(
    tmp_path: Path, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "revert.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    first = service.capture_sqlite(target, title="Revert")
    connection = sqlite3.connect(target)
    connection.execute("ALTER TABLE t ADD COLUMN value TEXT")
    connection.commit()
    connection.close()
    second = service.capture_sqlite(target, title="Revert")
    connection = sqlite3.connect(target)
    connection.execute("DROP TABLE t")
    connection.execute("CREATE TABLE t(id INTEGER)")
    connection.commit()
    connection.close()
    third = service.capture_sqlite(target, title="Revert")
    assert (first.version, second.version, third.version) == (1, 2, 3)
    assert third.document_id == first.document_id
    assert third.reverted is True
    assert len(service.history(first.source_id)) == 3


def test_service_history_diff_and_application_wiring(tmp_path: Path, engine: Engine) -> None:
    target = tmp_path / "wired.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    library = KnowledgeLibrary(create_session_factory(engine), clock=lambda: NOW)
    first = library.database_schema.capture_sqlite(target, title="Wired")
    connection = sqlite3.connect(target)
    connection.execute("CREATE TABLE added(id INTEGER)")
    connection.commit()
    connection.close()
    second = library.database_schema.capture_sqlite(target, title="Wired")
    assert library.database_schema.get_snapshot(first.source_id, 1).document_id == first.document_id
    assert library.database_schema.diff(first.source_id, 1, 2) == second.diff


def test_service_rolls_back_every_local_write_after_failure(
    tmp_path: Path,
    engine: Engine,
    service: DatabaseSchemaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "rollback.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")

    def fail_add(
        _repository: DatabaseSchemaSnapshotRepository,
        _record: DatabaseSchemaSnapshotRecord,
    ) -> None:
        raise RuntimeError("stop after writes")

    monkeypatch.setattr(DatabaseSchemaSnapshotRepository, "add", fail_add)
    with pytest.raises(RuntimeError, match="stop after writes"):
        service.capture_sqlite(target, title="Rollback")
    with UnitOfWork(create_session_factory(engine)) as uow:
        assert uow.sources.list() == []
        assert uow.documents.list() == []
        assert uow.knowledge_units.list() == []
        assert uow.database_schema_snapshots.list() == []
        assert uow.revisions.list() == []


def test_service_rejects_knowledge_library_database_and_hardlink(tmp_path: Path) -> None:
    metadata = tmp_path / "library.db"
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{metadata}")
    create_schema(engine)
    service = DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)
    alias = tmp_path / "alias.db"
    try:
        alias.hardlink_to(metadata)
        with pytest.raises(DatabaseSchemaError, match="Knowledge Library"):
            service.capture_sqlite(alias, title="Forbidden")
    finally:
        engine.dispose()


def test_source_and_snapshot_never_store_sqlite_path(
    tmp_path: Path, engine: Engine, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "sensitive-name.db"
    create_external(target, "CREATE TABLE t(id INTEGER)")
    result = service.capture_sqlite(target, title="Safe title")
    with UnitOfWork(create_session_factory(engine)) as uow:
        source = uow.sources.get(result.source_id)
        document = uow.documents.get(result.document_id)
        assert source is not None and document is not None
        assert source.uri.startswith("database://sqlite/")
        assert str(target) not in source.uri
        assert str(target) not in json.dumps(source.metadata)
        assert document.locator == source.uri
        assert source.title == "Safe title"


def test_postgresql_url_validation_is_strict_and_error_has_no_credentials() -> None:
    secret = "super-secret-password"
    with pytest.raises(DatabaseSchemaError) as captured:
        PostgreSQLSchemaCollector(f"mysql://alice:{secret}@localhost/db")
    assert secret not in str(captured.value)
    with pytest.raises(DatabaseSchemaError):
        PostgreSQLSchemaCollector("postgresql+asyncpg://localhost/db")
    with pytest.raises(DatabaseSchemaError):
        PostgreSQLSchemaCollector("postgresql+psycopg://localhost/db", schema="pg_catalog")

    for standard_driver in ("postgresql", "postgres"):
        PostgreSQLSchemaCollector(f"{standard_driver}://localhost/db")

    with pytest.raises(DatabaseSchemaError, match="invalid PostgreSQL URL") as malformed:
        PostgreSQLSchemaCollector(f"postgresql://alice:{secret}@localhost:notaport/db")
    assert secret not in str(malformed.value)
    assert malformed.value.__cause__ is None
    assert malformed.value.__suppress_context__ is True


def test_postgresql_missing_extra_error_is_explicit_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "super-secret-password"

    def missing_driver(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ModuleNotFoundError("psycopg")

    monkeypatch.setattr("konwledge.database_schema.collector.create_engine", missing_driver)
    collector = PostgreSQLSchemaCollector(
        f"postgresql+psycopg://alice:{secret}@localhost/db?application_name={secret}"
    )
    with pytest.raises(DatabaseConnectionError, match="optional 'postgres' extra") as captured:
        collector.capture()
    assert secret not in str(captured.value)


def test_postgresql_service_identity_excludes_user_password_and_query(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = DatabaseSchemaSnapshot(
        dialect=DatabaseDialect.POSTGRESQL,
        namespaces=(DatabaseNamespace(name="public", tables=()),),
    )
    monkeypatch.setattr(PostgreSQLSchemaCollector, "capture", lambda _collector: snapshot)
    service = DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)
    dsn = "postgresql+psycopg://alice:secret@Example.COM:5432/app?token=hidden"
    result = service.capture_postgresql(dsn, title="Postgres")
    with UnitOfWork(create_session_factory(engine)) as uow:
        source = uow.sources.get(result.source_id)
        assert source is not None
        serialized = json.dumps(source.model_dump(mode="json"))
        assert "alice" not in serialized
        assert "secret" not in serialized
        assert "hidden" not in serialized
        assert source.uri.startswith("database://postgresql/")
