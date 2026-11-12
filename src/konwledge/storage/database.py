"""SQLite engine and session helpers."""

from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from konwledge.storage.full_text import create_full_text_schema, fts_document
from konwledge.storage.tables import Base

SessionFactory = sessionmaker[Session]


def create_sqlite_engine(
    url: str = "sqlite+pysqlite:///konwledge.db", *, echo: bool = False
) -> Engine:
    """Create an SQLite engine with foreign keys and FTS helpers enabled.

    In-memory databases use a static pool so every session shares the same database.
    """

    kwargs: dict[str, object] = {"echo": echo}
    if url in {"sqlite://", "sqlite:///:memory:", "sqlite+pysqlite:///:memory:"}:
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(
        dbapi_connection: sqlite3.Connection, _connection_record: object
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function(
            "knowledge_fts_text",
            1,
            fts_document,
            deterministic=True,
        )

    return engine


def create_schema(engine: Engine) -> None:
    """Create missing tables and apply additive upgrades to older local databases.

    ``create_all`` never alters existing tables, so columns introduced after a
    database was first created are added here. Only nullable, additive changes
    are allowed, which keeps upgrades safe to run on every CLI start.
    """

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (("knowledge_units", "lineage_id"),)


def _add_missing_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table_name, column_name in _ADDITIVE_COLUMNS:
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            if column_name in existing:
                continue
            table = Base.metadata.tables[table_name]
            column = table.columns[column_name]
            column_type = column.type.compile(dialect=engine.dialect)
            connection.execute(
                text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}')
            )
            for index in table.indexes:
                if [indexed.name for indexed in index.columns] == [column_name]:
                    index.create(connection, checkfirst=True)
        create_full_text_schema(connection)


def create_session_factory(engine: Engine) -> SessionFactory:
    """Build hooked sessions with explicit completion and stable entity state."""

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Import lazily to avoid a storage/service import cycle. Construction installs
    # the transaction hooks but performs no writes and remains disabled by default.
    from konwledge.auto_evaluation import AutoEvaluationService

    AutoEvaluationService(factory)
    return factory
