"""SQLite engine and session helpers."""

from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, create_engine, event
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
    """Create all metadata tables if they do not already exist."""

    Base.metadata.create_all(engine)


def create_session_factory(engine: Engine) -> SessionFactory:
    """Build sessions with explicit transaction completion and stable entity state."""

    return sessionmaker(bind=engine, expire_on_commit=False)
