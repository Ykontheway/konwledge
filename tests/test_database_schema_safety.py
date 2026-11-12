"""U20 adversarial and transactional safety tests for database-schema capture."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import konwledge.database_schema.models as schema_models
from konwledge.application import KnowledgeLibrary
from konwledge.database_schema import (
    DatabaseColumn,
    DatabaseDialect,
    DatabaseNamespace,
    DatabaseObjectType,
    DatabaseSchemaError,
    DatabaseSchemaLimitError,
    DatabaseSchemaService,
    DatabaseSchemaSnapshot,
    DatabaseSchemaSnapshotRecord,
    DatabaseTable,
    SchemaCollector,
    SQLiteSchemaCollector,
    dump_database_schema_json,
    load_database_schema_json,
    render_database_schema,
)
from konwledge.storage import (
    DatabaseSchemaSnapshotRepository,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
SENTINEL = "U20-BUSINESS-ROW-DO-NOT-CAPTURE"


def create_external(path: Path, ddl: str, *, row: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ddl)
        if row:
            connection.execute("INSERT INTO sentinel(payload) VALUES (?)", (SENTINEL,))
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    value = create_sqlite_engine(f"sqlite+pysqlite:///{tmp_path / 'library.sqlite'}")
    create_schema(value)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def service(engine: Engine) -> DatabaseSchemaService:
    return DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)


def test_missing_target_is_rejected_without_file_creation(tmp_path: Path) -> None:
    target = tmp_path / "missing.sqlite"
    with pytest.raises(DatabaseSchemaError, match="does not exist"):
        SQLiteSchemaCollector(target)
    assert not target.exists()


@pytest.mark.parametrize("timeout", [0.0, -1.0, 60.0001, math.inf, math.nan])
def test_timeout_must_be_finite_and_within_documented_bounds(
    tmp_path: Path, timeout: float
) -> None:
    target = tmp_path / "target.sqlite"
    create_external(target, "")
    with pytest.raises(DatabaseSchemaError, match="timeout_seconds"):
        SQLiteSchemaCollector(target, timeout_seconds=timeout)


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_service_rejects_every_alias_of_its_own_metadata_database(
    tmp_path: Path, engine: Engine, alias_kind: str
) -> None:
    metadata = tmp_path / "library.sqlite"
    alias = tmp_path / f"metadata-{alias_kind}.sqlite"
    if alias_kind == "symlink":
        alias.symlink_to(metadata)
    else:
        alias.hardlink_to(metadata)
    before = metadata.read_bytes()

    library = DatabaseSchemaService(create_session_factory(engine), clock=lambda: NOW)
    with pytest.raises(DatabaseSchemaError, match="Knowledge Library"):
        library.capture_sqlite(alias, title="must fail")
    assert metadata.read_bytes() == before


def test_literal_uri_metacharacters_in_filename_cannot_redirect_capture(tmp_path: Path) -> None:
    target = tmp_path / "literal?#%&mode=rw.sqlite"
    decoy = tmp_path / "literal"
    create_external(target, 'CREATE TABLE "real"(id INTEGER)')
    create_external(decoy, 'CREATE TABLE "decoy"(id INTEGER)')

    snapshot = SQLiteSchemaCollector(target).capture()
    assert [table.name for table in snapshot.namespaces[0].tables] == ["real"]
    assert decoy.exists()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT payload FROM sentinel",
        "SELECT count(*) FROM sentinel",
        "INSERT INTO sentinel(payload) VALUES ('escaped')",
        "UPDATE sentinel SET payload = 'escaped'",
        "DELETE FROM sentinel",
        "CREATE TABLE escaped(id INTEGER)",
        "DROP TABLE sentinel",
        "ATTACH DATABASE ':memory:' AS escaped",
        "PRAGMA writable_schema=ON",
        "SELECT hex('catalog')",
    ],
)
def test_sqlite_authorizer_denies_business_reads_writes_ddl_attach_and_functions(
    tmp_path: Path, statement: str
) -> None:
    target = tmp_path / "authorizer.sqlite"
    create_external(target, "CREATE TABLE sentinel(payload TEXT)", row=True)

    class Probe(SQLiteSchemaCollector):
        def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
            connection.execute(statement).fetchall()
            raise AssertionError("adversarial SQL unexpectedly escaped the authorizer")

    with pytest.raises(DatabaseSchemaError, match="capture failed"):
        Probe(target).capture()
    check = sqlite3.connect(target)
    try:
        assert check.execute("SELECT payload FROM sentinel").fetchall() == [(SENTINEL,)]
        assert check.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='escaped'"
        ).fetchone() == (0,)
    finally:
        check.close()


def test_sqlite_catalog_query_uses_only_catalog_reads_and_metadata_pragmas(tmp_path: Path) -> None:
    target = tmp_path / "trace.sqlite"
    create_external(
        target,
        "CREATE TABLE sentinel(payload TEXT); CREATE INDEX sentinel_idx ON sentinel(payload)",
        row=True,
    )
    statements: list[str] = []

    class Traced(SQLiteSchemaCollector):
        def _collect(self, connection: sqlite3.Connection) -> DatabaseSchemaSnapshot:
            connection.set_trace_callback(statements.append)
            return super()._collect(connection)

    snapshot = Traced(target).capture()
    rendered = snapshot.canonical_json()
    assert SENTINEL not in rendered
    assert statements
    normalized = "\n".join(statements).casefold()
    assert "select payload" not in normalized
    assert " from sentinel" not in normalized
    assert not any(
        token in normalized
        for token in ("insert into", "update sentinel", "delete from", "attach database")
    )
    assert "sqlite_master" in normalized and "pragma table_xinfo" in normalized


def test_injection_shaped_unicode_and_quoted_identifiers_are_inert_data(tmp_path: Path) -> None:
    target = tmp_path / "quoted.sqlite"
    table_name = '用户"; DROP TABLE sentinel;--'
    column_name = "值'); ATTACH DATABASE 'x' AS y;--"
    create_external(
        target,
        f'CREATE TABLE "{table_name.replace(chr(34), chr(34) * 2)}"'
        f'("{column_name}" TEXT); CREATE TABLE sentinel(payload TEXT)',
        row=True,
    )
    snapshot = SQLiteSchemaCollector(target).capture()
    tables = {table.name: table for table in snapshot.namespaces[0].tables}
    assert table_name in tables and "sentinel" in tables
    assert tables[table_name].columns[0].name == column_name
    assert SENTINEL not in dump_database_schema_json(snapshot)


def test_sqlite_composite_foreign_keys_and_generated_columns_are_complete(tmp_path: Path) -> None:
    target = tmp_path / "complex.sqlite"
    create_external(
        target,
        """
        CREATE TABLE parent(a INTEGER, b INTEGER, PRIMARY KEY(a, b));
        CREATE TABLE child(
          a INTEGER,
          b INTEGER,
          generated_value TEXT GENERATED ALWAYS AS (a || ':' || b) STORED,
          FOREIGN KEY(a, b) REFERENCES parent(a, b) ON UPDATE CASCADE ON DELETE RESTRICT
        );
        """,
    )
    child = {
        table.name: table for table in SQLiteSchemaCollector(target).capture().namespaces[0].tables
    }["child"]
    assert child.columns[2].generated is True
    assert child.foreign_keys[0].columns == ("a", "b")
    assert child.foreign_keys[0].referred_columns == ("a", "b")
    assert child.foreign_keys[0].on_update == "CASCADE"
    assert child.foreign_keys[0].on_delete == "RESTRICT"


def test_sqlite_expression_index_preserves_expression_sql(tmp_path: Path) -> None:
    """Regression: collector.py:_table currently discards SQLite index expressions."""

    target = tmp_path / "expression.sqlite"
    create_external(
        target, "CREATE TABLE item(name TEXT); CREATE INDEX item_lower ON item(lower(name))"
    )
    table = SQLiteSchemaCollector(target).capture().namespaces[0].tables[0]
    term = next(index for index in table.indexes if index.name == "item_lower").terms[0]
    assert term.name is None
    assert term.expression is not None and "lower" in term.expression.casefold()


def test_sqlite_partial_index_preserves_predicate(tmp_path: Path) -> None:
    """Regression: collector.py:_table currently hard-codes every predicate to None."""

    target = tmp_path / "partial.sqlite"
    create_external(
        target,
        "CREATE TABLE item(id INTEGER, active INTEGER); "
        "CREATE UNIQUE INDEX active_item ON item(id) WHERE active = 1",
    )
    table = SQLiteSchemaCollector(target).capture().namespaces[0].tables[0]
    index = next(index for index in table.indexes if index.name == "active_item")
    assert index.unique is True
    assert index.predicate is not None and "active" in index.predicate


def test_table_bound_rejects_whole_capture_instead_of_returning_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tables.sqlite"
    create_external(target, "CREATE TABLE a(id INTEGER); CREATE TABLE b(id INTEGER)")
    monkeypatch.setattr(schema_models, "MAX_TABLES", 1)
    with pytest.raises(ValidationError, match="more than 1 tables"):
        SQLiteSchemaCollector(target).capture()


def test_column_bound_rejects_whole_capture_instead_of_returning_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "columns.sqlite"
    create_external(target, "CREATE TABLE a(first INTEGER, second INTEGER)")
    monkeypatch.setattr(schema_models, "MAX_COLUMNS", 1)
    with pytest.raises((DatabaseSchemaLimitError, ValidationError)):
        SQLiteSchemaCollector(target).capture()


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_schema_version_rejects_non_integer_one(value: object) -> None:
    with pytest.raises(ValidationError):
        DatabaseSchemaSnapshot.model_validate(
            {"schema_version": value, "dialect": "sqlite", "namespaces": []}
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_loader_rejects_every_nonfinite_constant(constant: str) -> None:
    payload = '{"schema_version":1,"dialect":"sqlite","namespaces":[],"extra":' + constant + "}"
    with pytest.raises(DatabaseSchemaError, match="non-finite"):
        load_database_schema_json(payload)


def test_snapshot_record_rejects_naive_and_non_datetime_timestamps() -> None:
    snapshot = DatabaseSchemaSnapshot(
        dialect=DatabaseDialect.SQLITE,
        namespaces=(DatabaseNamespace(name="main", tables=()),),
    )
    base = {
        "source_id": uuid4(),
        "version": 1,
        "snapshot": snapshot,
        "content_hash": snapshot.content_hash,
        "document_id": uuid4(),
    }
    for invalid in (NOW.replace(tzinfo=None), True, 1, "not-a-timestamp"):
        with pytest.raises(ValidationError):
            DatabaseSchemaSnapshotRecord.model_validate({**base, "captured_at": invalid})


def test_failure_after_snapshot_repository_write_rolls_back_every_entity(
    tmp_path: Path,
    engine: Engine,
    service: DatabaseSchemaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "rollback.sqlite"
    create_external(target, "CREATE TABLE item(id INTEGER)")
    original = DatabaseSchemaSnapshotRepository.add

    def fail_after_add(
        repository: DatabaseSchemaSnapshotRepository, record: DatabaseSchemaSnapshotRecord
    ) -> None:
        original(repository, record)
        raise RuntimeError("failure after snapshot flush")

    monkeypatch.setattr(DatabaseSchemaSnapshotRepository, "add", fail_after_add)
    with pytest.raises(RuntimeError, match="after snapshot flush"):
        service.capture_sqlite(target, title="atomic")

    with UnitOfWork(create_session_factory(engine)) as uow:
        assert uow.sources.list() == []
        assert uow.documents.list() == []
        assert uow.knowledge_units.list() == []
        assert uow.revisions.list() == []
        assert uow.database_schema_snapshots.list() == []


def test_failure_after_publication_still_rolls_back_all_local_writes(
    tmp_path: Path,
    engine: Engine,
    service: DatabaseSchemaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "publish-rollback.sqlite"
    create_external(target, "CREATE TABLE item(id INTEGER)")

    def fail_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("failure after publication")

    monkeypatch.setattr(
        "konwledge.database_schema.service.materialize_statuses", fail_materialization
    )
    with pytest.raises(RuntimeError, match="after publication"):
        service.capture_sqlite(target, title="atomic")

    with UnitOfWork(create_session_factory(engine)) as uow:
        assert not any(
            (
                uow.sources.list(),
                uow.documents.list(),
                uow.knowledge_units.list(),
                uow.revisions.list(),
                uow.database_schema_snapshots.list(),
            )
        )


def test_a_b_a_reuses_document_but_appends_history_without_duplicate_revisions(
    tmp_path: Path, engine: Engine, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "revert.sqlite"
    create_external(target, "CREATE TABLE item(id INTEGER)")
    first = service.capture_sqlite(target, title="revert")
    connection = sqlite3.connect(target)
    connection.execute("ALTER TABLE item ADD COLUMN changed TEXT")
    connection.commit()
    connection.close()
    second = service.capture_sqlite(target, title="revert")
    connection = sqlite3.connect(target)
    connection.execute("DROP TABLE item")
    connection.execute("CREATE TABLE item(id INTEGER)")
    connection.commit()
    connection.close()
    third = service.capture_sqlite(target, title="revert")

    assert (first.version, second.version, third.version) == (1, 2, 3)
    assert third.document_id == first.document_id != second.document_id
    assert third.reverted is True
    with UnitOfWork(create_session_factory(engine)) as uow:
        assert len(uow.documents.list()) == 2
        revisions = uow.revisions.list()
        revision_keys = {(item.entity_type, item.entity_id, item.revision) for item in revisions}
        assert len(revision_keys) == len(revisions)
        assert len(uow.database_schema_snapshots.list()) == 3


def test_render_and_persistence_never_include_business_rows_or_external_path(
    tmp_path: Path, engine: Engine, service: DatabaseSchemaService
) -> None:
    target = tmp_path / "credentials-secret-location.sqlite"
    create_external(target, "CREATE TABLE sentinel(payload TEXT)", row=True)
    result = service.capture_sqlite(target, title="safe")
    rendered, _ = render_database_schema(result.snapshot)
    assert SENTINEL not in rendered

    with UnitOfWork(create_session_factory(engine)) as uow:
        persisted = json.dumps(
            {
                "sources": [item.model_dump(mode="json") for item in uow.sources.list()],
                "documents": [item.model_dump(mode="json") for item in uow.documents.list()],
                "units": [item.model_dump(mode="json") for item in uow.knowledge_units.list()],
                "revisions": [item.model_dump(mode="json") for item in uow.revisions.list()],
                "snapshots": [
                    item.model_dump(mode="json") for item in uow.database_schema_snapshots.list()
                ],
            },
            default=str,
        )
    assert SENTINEL not in persisted
    assert str(target) not in persisted


def test_concurrent_initial_capture_never_leaves_partial_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A losing first-capture race may fail, but must not commit a partial graph."""

    target = tmp_path / "race-target.sqlite"
    metadata = tmp_path / "race-library.sqlite"
    create_external(target, "CREATE TABLE item(id INTEGER)")
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{metadata}")
    create_schema(engine)
    factory = create_session_factory(engine)
    barrier = threading.Barrier(2)
    original_capture = SQLiteSchemaCollector.capture

    def synchronized_capture(collector: SQLiteSchemaCollector) -> DatabaseSchemaSnapshot:
        snapshot = original_capture(collector)
        barrier.wait(timeout=10)
        return snapshot

    monkeypatch.setattr(SQLiteSchemaCollector, "capture", synchronized_capture)
    errors: list[BaseException] = []
    results: list[object] = []

    def worker() -> None:
        try:
            results.append(
                DatabaseSchemaService(factory, clock=lambda: NOW).capture_sqlite(
                    target, title="race"
                )
            )
        except BaseException as exc:  # the safety invariant is checked after either outcome
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert results or errors

    with UnitOfWork(factory) as uow:
        sources = uow.sources.list()
        documents = uow.documents.list()
        units = uow.knowledge_units.list()
        records = uow.database_schema_snapshots.list()
        assert len(sources) == len(documents) == len(units) == len(records) == 1
        assert documents[0].source_id == sources[0].id
        assert units[0].document_id == documents[0].id
        assert records[0].document_id == documents[0].id
    engine.dispose()


def test_capture_with_failure_occurs_before_any_local_transaction(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = create_session_factory(engine)
    service = DatabaseSchemaService(factory, clock=lambda: NOW)

    class FailingCollector(SchemaCollector):
        def capture(self) -> DatabaseSchemaSnapshot:
            raise DatabaseSchemaError("synthetic collector failed")

    opened = False

    def forbidden_factory() -> object:
        nonlocal opened
        opened = True
        raise AssertionError("local transaction opened before external capture completed")

    monkeypatch.setattr(service, "_session_factory", forbidden_factory)
    with pytest.raises(DatabaseSchemaError, match="synthetic collector failed"):
        service.capture_with(FailingCollector(), identity="synthetic", title="failure")
    assert opened is False


def test_model_rejects_noncontiguous_ordinals_and_duplicate_names() -> None:
    columns = (
        DatabaseColumn(name="same", data_type="INTEGER", nullable=True, ordinal=0),
        DatabaseColumn(name="same", data_type="INTEGER", nullable=True, ordinal=2),
    )
    with pytest.raises(ValidationError):
        DatabaseTable(
            namespace="main",
            name="bad",
            object_type=DatabaseObjectType.TABLE,
            columns=columns,
        )


def test_view_definition_only_change_materializes_new_revision(
    tmp_path: Path, service: DatabaseSchemaService, engine: Engine
) -> None:
    target = tmp_path / "views.sqlite"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE VIEW visible_schema AS SELECT 1 AS stable_column")
    first = service.capture_sqlite(target, title="Synthetic views")
    with sqlite3.connect(target) as connection:
        connection.execute("DROP VIEW visible_schema")
        connection.execute("CREATE VIEW visible_schema AS SELECT 2 AS stable_column")
    second = service.capture_sqlite(target, title="Synthetic views")

    assert second.version == first.version + 1
    assert second.document_id != first.document_id
    assert second.content_hash != first.content_hash
    assert second.diff is not None
    assert second.diff.changed_tables[0].options_changed is True
    library = KnowledgeLibrary(create_session_factory(engine), clock=lambda: NOW)
    packet = library.build_context("visible_schema")
    assert len(packet.blocks) == 1
    assert "SELECT 2 AS stable_column" in packet.context_text
    assert "SELECT 1 AS stable_column" not in packet.context_text
    repeat = service.capture_sqlite(target, title="Synthetic views")
    assert repeat.created is False
    assert repeat.version == second.version
