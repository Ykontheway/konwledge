"""U20 live PostgreSQL catalog tests.

Set ``KNOWLEDGE_TEST_POSTGRES_DSN`` to an administrator URL for an isolated
server.  The module creates and drops its own database, roles, and per-test
schemas; no pre-existing application database is inspected.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from konwledge.application import KnowledgeLibrary
from konwledge.database_schema import (
    DatabaseConnectionError,
    DatabaseObjectType,
    DatabaseSchemaService,
    PostgreSQLSchemaCollector,
    dump_database_schema_json,
)
from konwledge.domain import Citation, RevisionEntityType, SourceKind
from konwledge.storage import (
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

ADMIN_DSN = os.environ.get("KNOWLEDGE_TEST_POSTGRES_DSN")
if not ADMIN_DSN:
    pytest.skip(
        "live PostgreSQL tests require KNOWLEDGE_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

try:
    psycopg = importlib.import_module("psycopg")
    sql = importlib.import_module("psycopg.sql")
except ImportError:
    pytest.fail(
        "KNOWLEDGE_TEST_POSTGRES_DSN is set but psycopg 3 is not installed; "
        "live coverage must not be silently skipped",
        pytrace=False,
    )

SENTINEL = "U20-LIVE-BUSINESS-ROW-MUST-NEVER-APPEAR"


@dataclass(frozen=True)
class LivePostgres:
    admin_url: str
    readonly_url: str
    second_url: str
    database: str
    readonly_role: str
    second_role: str
    password: str
    second_password: str
    server_version: int


def _connect(url: str, *, autocommit: bool = True) -> Any:
    parsed = make_url(url)
    return psycopg.connect(
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 5432,
        dbname=parsed.database or "postgres",
        user=parsed.username,
        password=parsed.password,
        connect_timeout=5,
        autocommit=autocommit,
    )


def _url_for(
    base: str,
    *,
    database: str,
    username: str | None = None,
    password: str | None = None,
    query: dict[str, str] | None = None,
    driver: str = "postgresql+psycopg",
) -> str:
    parsed = make_url(base).set(
        drivername=driver,
        database=database,
        username=username if username is not None else make_url(base).username,
        password=password if password is not None else make_url(base).password,
        query=query or {},
    )
    return parsed.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def live_postgres() -> Iterator[LivePostgres]:
    assert ADMIN_DSN is not None
    suffix = uuid4().hex[:12]
    database = f"u20_catalog_{suffix}"
    readonly_role = f"u20_reader_{suffix}"
    second_role = f"u20_reader2_{suffix}"
    password = f"u20-pass-{suffix}"
    second_password = f"u20-pass2-{suffix}"
    root_database = make_url(ADMIN_DSN).database or "postgres"

    with _connect(ADMIN_DSN) as connection:
        server_version = int(connection.execute("SHOW server_version_num").fetchone()[0])
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
            ).format(sql.Identifier(readonly_role), sql.Literal(password))
        )
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
            ).format(sql.Identifier(second_role), sql.Literal(second_password))
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(database),
                sql.Identifier(readonly_role),
                sql.Identifier(second_role),
            )
        )

    admin_url = _url_for(ADMIN_DSN, database=database)
    readonly_url = _url_for(
        ADMIN_DSN,
        database=database,
        username=readonly_role,
        password=password,
        query={"application_name": "u20-readonly"},
    )
    second_url = _url_for(
        ADMIN_DSN,
        database=database,
        username=second_role,
        password=second_password,
        query={"application_name": "u20-second"},
    )
    fixture = LivePostgres(
        admin_url=admin_url,
        readonly_url=readonly_url,
        second_url=second_url,
        database=database,
        readonly_role=readonly_role,
        second_role=second_role,
        password=password,
        second_password=second_password,
        server_version=server_version,
    )
    try:
        yield fixture
    finally:
        with _connect(_url_for(ADMIN_DSN, database=root_database)) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
            )
            connection.execute(
                sql.SQL("DROP ROLE IF EXISTS {}, {}").format(
                    sql.Identifier(readonly_role), sql.Identifier(second_role)
                )
            )


@pytest.fixture
def pg_schema(live_postgres: LivePostgres) -> Iterator[str]:
    name = f"u20_{uuid4().hex[:16]}"
    with _connect(live_postgres.admin_url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                sql.Identifier(name),
                sql.Identifier(live_postgres.readonly_role),
                sql.Identifier(live_postgres.second_role),
            )
        )
    try:
        yield name
    finally:
        with _connect(live_postgres.admin_url) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
            )


def _execute_ddl(live: LivePostgres, statements: str) -> None:
    with _connect(live.admin_url, autocommit=False) as connection:
        connection.execute(statements)
        connection.commit()


def _tables(snapshot: Any) -> dict[str, Any]:
    return {table.name: table for table in snapshot.namespaces[0].tables}


def test_server_is_isolated_loopback_and_supported_postgresql(live_postgres: LivePostgres) -> None:
    parsed = make_url(live_postgres.admin_url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert live_postgres.database.startswith("u20_catalog_")
    # Production CI targets 16-18; PostgreSQL 15 is accepted here to keep SQL compatibility honest.
    assert live_postgres.server_version >= 150000


def test_low_privilege_role_has_no_business_table_select_but_catalog_is_visible(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(
        live_postgres,
        f'CREATE TABLE "{pg_schema}".private_data(id integer primary key, payload text); '
        f"INSERT INTO \"{pg_schema}\".private_data VALUES (1, '{SENTINEL}')",
    )
    with _connect(live_postgres.admin_url) as connection:
        capabilities = connection.execute(
            "SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
            "has_table_privilege(%s, %s, 'SELECT') "
            "FROM pg_roles r WHERE r.rolname = %s",
            (
                live_postgres.readonly_role,
                f"{pg_schema}.private_data",
                live_postgres.readonly_role,
            ),
        ).fetchone()
    assert capabilities == (False, False, False, False)

    snapshot = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    assert "private_data" in _tables(snapshot)
    assert SENTINEL not in snapshot.canonical_json()


def test_collects_comments_pk_fk_unique_expression_partial_generated_identity_and_view(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(
        live_postgres,
        f"""
        CREATE TABLE "{pg_schema}".parent (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          code text NOT NULL UNIQUE,
          note text DEFAULT 'metadata default'
        );
        COMMENT ON TABLE "{pg_schema}".parent IS '父表 comment';
        COMMENT ON COLUMN "{pg_schema}".parent.code IS '业务编码 comment';
        CREATE TABLE "{pg_schema}".child (
          id bigint PRIMARY KEY,
          parent_id bigint NOT NULL REFERENCES "{pg_schema}".parent(id)
            ON UPDATE CASCADE ON DELETE RESTRICT,
          amount integer NOT NULL,
          doubled integer GENERATED ALWAYS AS (amount * 2) STORED
        );
        CREATE UNIQUE INDEX child_lower_unique
          ON "{pg_schema}".child ((lower(id::text)));
        CREATE INDEX child_positive_partial
          ON "{pg_schema}".child (amount DESC) WHERE amount > 0;
        CREATE VIEW "{pg_schema}".child_view AS
          SELECT id, parent_id, doubled FROM "{pg_schema}".child;
        INSERT INTO "{pg_schema}".parent(code, note)
          OVERRIDING SYSTEM VALUE VALUES ('business-code', '{SENTINEL}');
        """,
    )
    snapshot = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    tables = _tables(snapshot)
    assert set(tables) == {"child", "child_view", "parent"}
    assert tables["child_view"].object_type is DatabaseObjectType.VIEW
    assert tables["parent"].comment == "父表 comment"
    parent_columns = {column.name: column for column in tables["parent"].columns}
    assert parent_columns["id"].generated is True
    assert parent_columns["id"].primary_key_position == 1
    assert parent_columns["code"].nullable is False
    assert parent_columns["code"].comment == "业务编码 comment"
    child_columns = {column.name: column for column in tables["child"].columns}
    assert child_columns["doubled"].generated is True
    foreign_key = tables["child"].foreign_keys[0]
    assert foreign_key.columns == ("parent_id",)
    assert foreign_key.referred_table == "parent"
    assert foreign_key.referred_columns == ("id",)
    assert foreign_key.on_update == "CASCADE"
    assert foreign_key.on_delete == "RESTRICT"
    expression = next(
        index for index in tables["child"].indexes if index.name == "child_lower_unique"
    )
    assert expression.unique is True
    assert expression.terms[0].expression is not None
    partial = next(
        index for index in tables["child"].indexes if index.name == "child_positive_partial"
    )
    assert partial.predicate is not None and "amount" in partial.predicate
    assert SENTINEL not in snapshot.canonical_json()


def test_unicode_and_quoted_identifiers_are_collected_as_data(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    table_name = '用户"表; DROP SCHEMA public;--'
    column_name = '列"名); SELECT secret;--'
    with _connect(live_postgres.admin_url) as connection:
        connection.execute(
            sql.SQL("CREATE TABLE {}.{} ({} text PRIMARY KEY)").format(
                sql.Identifier(pg_schema),
                sql.Identifier(table_name),
                sql.Identifier(column_name),
            )
        )
    snapshot = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    table = _tables(snapshot)[table_name]
    assert table.columns[0].name == column_name
    assert table.columns[0].primary_key_position == 1


def test_live_capture_is_byte_for_byte_deterministic(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(
        live_postgres,
        f'CREATE TABLE "{pg_schema}".z(id integer); CREATE TABLE "{pg_schema}".a(id text)',
    )
    collector = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema)
    first = collector.capture()
    second = collector.capture()
    assert first.content_hash == second.content_hash
    assert first.canonical_json() == second.canonical_json()
    assert dump_database_schema_json(first) == dump_database_schema_json(second)


def test_collector_executes_catalog_sql_only_and_never_fetches_business_rows(
    live_postgres: LivePostgres,
    pg_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execute_ddl(
        live_postgres,
        f'CREATE TABLE "{pg_schema}".sentinel(payload text); '
        f"INSERT INTO \"{pg_schema}\".sentinel VALUES ('{SENTINEL}')",
    )
    import konwledge.database_schema.collector as collector_module

    real_create_engine = create_engine
    executed: list[tuple[str, str]] = []

    def instrumented_engine(url: str | URL, **kwargs: Any) -> Engine:
        engine = real_create_engine(url, **kwargs)

        @event.listens_for(engine, "before_cursor_execute")
        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del connection, cursor, context, executemany
            executed.append((statement, repr(parameters)))

        return engine

    monkeypatch.setattr(collector_module, "create_engine", instrumented_engine)
    snapshot = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    transcript = "\n".join(statement + parameters for statement, parameters in executed)
    assert executed and "SHOW transaction_read_only" in transcript
    assert SENTINEL not in transcript
    assert not re.search(
        rf'\bFROM\s+"?{re.escape(pg_schema)}"?\."?sentinel"?\b',
        transcript,
        flags=re.IGNORECASE,
    )
    assert all(
        not re.match(
            r"\s*(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b", statement, re.I
        )
        for statement, _ in executed
    )
    assert SENTINEL not in snapshot.canonical_json()


def test_transaction_is_repeatable_read_and_read_only(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(live_postgres, f'CREATE TABLE "{pg_schema}".probe(id integer)')
    observed: dict[str, str] = {}

    class TransactionProbe(PostgreSQLSchemaCollector):
        def _collect(self, inspector: Any) -> Any:
            observed["readonly"] = inspector.bind.execute(
                text("SHOW transaction_read_only")
            ).scalar_one()
            observed["isolation"] = inspector.bind.execute(
                text("SHOW transaction_isolation")
            ).scalar_one()
            return super()._collect(inspector)

    TransactionProbe(live_postgres.readonly_url, schema=pg_schema).capture()
    assert observed == {"readonly": "on", "isolation": "repeatable read"}


def test_forced_write_is_rejected_by_transaction_even_when_role_has_insert_privilege(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(
        live_postgres,
        f'CREATE TABLE "{pg_schema}".write_probe(value text); '
        f'GRANT INSERT ON "{pg_schema}".write_probe TO "{live_postgres.readonly_role}"',
    )
    observed_error: list[str] = []

    class WriteProbe(PostgreSQLSchemaCollector):
        def _collect(self, inspector: Any) -> Any:
            try:
                inspector.bind.execute(
                    text(f"INSERT INTO \"{pg_schema}\".write_probe(value) VALUES ('escaped')")
                )
            except DBAPIError as exc:
                observed_error.append(str(exc.orig))
                raise
            raise AssertionError("write unexpectedly succeeded")

    with pytest.raises(DatabaseConnectionError, match="capture failed"):
        WriteProbe(live_postgres.readonly_url, schema=pg_schema).capture()
    assert observed_error and "read-only transaction" in observed_error[0].casefold()
    with _connect(live_postgres.admin_url) as connection:
        assert connection.execute(
            sql.SQL("SELECT count(*) FROM {}.write_probe").format(sql.Identifier(pg_schema))
        ).fetchone() == (0,)


@pytest.mark.parametrize("driver", ["postgresql", "postgres"])
def test_postgresql_schemes_are_normalized_to_psycopg(
    live_postgres: LivePostgres, pg_schema: str, driver: str
) -> None:
    """Regression: collector.py:237 rejects SQLAlchemy's standard postgresql:// URL."""

    _execute_ddl(live_postgres, f'CREATE TABLE "{pg_schema}".scheme_probe(id integer)')
    candidate = _url_for(
        live_postgres.readonly_url,
        database=live_postgres.database,
        username=live_postgres.readonly_role,
        password=live_postgres.password,
        driver=driver,
    )
    expected = PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    assert PostgreSQLSchemaCollector(candidate, schema=pg_schema).capture() == expected


def test_view_definition_is_part_of_portable_capture(
    live_postgres: LivePostgres, pg_schema: str
) -> None:
    """Regression: collector.py:_object detects a view but never stores its definition."""

    _execute_ddl(
        live_postgres,
        f'CREATE TABLE "{pg_schema}".base(id integer); '
        f'CREATE VIEW "{pg_schema}".visible AS SELECT id FROM "{pg_schema}".base WHERE id > 0',
    )
    view = _tables(
        PostgreSQLSchemaCollector(live_postgres.readonly_url, schema=pg_schema).capture()
    )["visible"]
    payload = view.model_dump(mode="json")
    assert "view_definition" in payload
    assert "WHERE" in payload["view_definition"].upper()


def test_real_service_persists_history_diff_lineage_citation_and_asof(
    tmp_path: Path, live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(live_postgres, f'CREATE TABLE "{pg_schema}".service_table(id integer)')
    local_engine = create_sqlite_engine(f"sqlite+pysqlite:///{tmp_path / 'knowledge.sqlite'}")
    create_schema(local_engine)
    factory = create_session_factory(local_engine)
    library = KnowledgeLibrary(factory)
    try:
        first = library.database_schema.capture_postgresql(
            live_postgres.readonly_url, schema=pg_schema, title="live synthetic"
        )
        repeated = library.database_schema.capture_postgresql(
            live_postgres.readonly_url, schema=pg_schema, title="live synthetic"
        )
        assert repeated.created is False
        assert repeated.version == 1
        assert repeated.content_hash == first.content_hash

        with UnitOfWork(factory) as uow:
            first_unit = uow.knowledge_units.list_by_document(first.document_id)[0]
            citation = Citation(
                id=uuid4(),
                source_id=first.source_id,
                document_id=first.document_id,
                knowledge_unit_id=first_unit.id,
                quote=first_unit.text[: min(80, len(first_unit.text))],
                locator="database-schema://synthetic",
            )
            uow.citations.add(citation)
            uow.commit()

        _execute_ddl(
            live_postgres,
            f'ALTER TABLE "{pg_schema}".service_table ADD COLUMN changed text',
        )
        second = library.database_schema.capture_postgresql(
            live_postgres.readonly_url, schema=pg_schema, title="live synthetic"
        )
        assert second.version == 2 and second.diff is not None
        assert second.diff.changed_tables[0].added_columns == ("changed",)
        historical = library.snapshot(first.captured_at, include_retired=True)
        assert historical.sources[0].head is not None
        assert historical.sources[0].head.id == first.document_id
        assert [item.version for item in library.database_schema.history(first.source_id)] == [1, 2]
        assert library.database_schema.diff(first.source_id, 1, 2) == second.diff

        current = library.snapshot(include_retired=True)
        assert current.sources[0].source.kind is SourceKind.DATABASE
        with UnitOfWork(factory) as uow:
            old_unit = uow.knowledge_units.list_by_document(first.document_id)[0]
            new_unit = uow.knowledge_units.list_by_document(second.document_id)[0]
            assert old_unit.lineage_id == new_unit.lineage_id
            revisions = uow.revisions.list_for(
                RevisionEntityType.KNOWLEDGE_UNIT, old_unit.lineage_id
            )
            assert len(revisions) == 2
            assert uow.citations.get(citation.id) == citation
            assert len(uow.sources.list()) == 1
            assert len(uow.documents.list()) == 2
            assert len(uow.database_schema_snapshots.list()) == 2
    finally:
        local_engine.dispose()


def test_cross_credentials_and_query_parameters_have_one_identity_and_never_persist(
    tmp_path: Path, live_postgres: LivePostgres, pg_schema: str
) -> None:
    _execute_ddl(live_postgres, f'CREATE TABLE "{pg_schema}".identity_probe(id integer)')
    local_engine = create_sqlite_engine(f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite'}")
    create_schema(local_engine)
    factory = create_session_factory(local_engine)
    service = DatabaseSchemaService(factory)
    first_url = _url_for(
        live_postgres.readonly_url,
        database=live_postgres.database,
        username=live_postgres.readonly_role,
        password=live_postgres.password,
        query={"application_name": "secret-first", "connect_timeout": "2"},
    )
    second_url = _url_for(
        live_postgres.second_url,
        database=live_postgres.database,
        username=live_postgres.second_role,
        password=live_postgres.second_password,
        query={"application_name": "secret-second"},
    )
    try:
        first = service.capture_postgresql(first_url, schema=pg_schema, title="identity")
        second = service.capture_postgresql(second_url, schema=pg_schema, title="identity")
        assert first.source_id == second.source_id
        assert second.created is False and second.version == 1
        with UnitOfWork(factory) as uow:
            serialized = json.dumps(
                {
                    "sources": [item.model_dump(mode="json") for item in uow.sources.list()],
                    "documents": [item.model_dump(mode="json") for item in uow.documents.list()],
                    "units": [item.model_dump(mode="json") for item in uow.knowledge_units.list()],
                    "revisions": [item.model_dump(mode="json") for item in uow.revisions.list()],
                    "records": [
                        item.model_dump(mode="json")
                        for item in uow.database_schema_snapshots.list()
                    ],
                },
                default=str,
            )
        for forbidden in (
            live_postgres.readonly_role,
            live_postgres.second_role,
            live_postgres.password,
            live_postgres.second_password,
            "secret-first",
            "secret-second",
        ):
            assert forbidden not in serialized
    finally:
        local_engine.dispose()


def test_failed_connect_error_and_local_state_never_leak_credentials(
    tmp_path: Path, live_postgres: LivePostgres, pg_schema: str
) -> None:
    secret = "u20-failed-connect-password"
    dead_url = (
        f"postgresql+psycopg://failed-user:{secret}@127.0.0.1:1/"
        f"{live_postgres.database}?application_name={secret}"
    )
    local_path = tmp_path / "failed.sqlite"
    local_engine = create_sqlite_engine(f"sqlite+pysqlite:///{local_path}")
    create_schema(local_engine)
    factory = create_session_factory(local_engine)
    service = DatabaseSchemaService(factory)
    try:
        with pytest.raises(DatabaseConnectionError) as captured:
            service.capture_postgresql(dead_url, schema=pg_schema, title="failed")
        assert secret not in str(captured.value)
        assert "failed-user" not in str(captured.value)
        with UnitOfWork(factory) as uow:
            assert uow.sources.list() == []
            assert uow.documents.list() == []
            assert uow.knowledge_units.list() == []
            assert uow.revisions.list() == []
            assert uow.database_schema_snapshots.list() == []
        assert secret.encode() not in local_path.read_bytes()
    finally:
        local_engine.dispose()
