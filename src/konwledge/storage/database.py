"""SQLite engine and session helpers."""

from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from konwledge.storage.tables import Base

SessionFactory = sessionmaker[Session]


def create_sqlite_engine(
    url: str = "sqlite+pysqlite:///konwledge.db", *, echo: bool = False
) -> Engine:
    """Create an SQLite engine with foreign keys enabled.

    In-memory databases use a static pool so every session shares the same database.
    """

    kwargs: dict[str, object] = {"echo": echo}
    if url in {"sqlite://", "sqlite:///:memory:", "sqlite+pysqlite:///:memory:"}:
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(
        dbapi_connection: sqlite3.Connection, _connection_record: object
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def create_session_factory(engine: Engine) -> SessionFactory:
    """Build sessions with explicit transaction completion and stable entity state."""

    return sessionmaker(bind=engine, expire_on_commit=False)
