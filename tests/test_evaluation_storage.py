from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, inspect, text

from konwledge.domain import EvaluationItem, EvaluationSet
from konwledge.storage import (
    ImmutableEntityError,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.full_text import create_full_text_schema
from konwledge.storage.tables import Base, EvaluationSetRow

SET_ID = UUID("10000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("20000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


def make_set(version: int = 1) -> EvaluationSet:
    item = EvaluationItem(
        id="case-1",
        query="Test query",
        expected_lineage_ids=frozenset({LINEAGE_ID}),
    )
    return EvaluationSet(
        id=SET_ID,
        name="Test Set",
        version=version,
        created_at=NOW,
        items=(item,),
    )


def test_evaluation_repository_roundtrip(session_factory: SessionFactory) -> None:
    ds = make_set()
    with UnitOfWork(session_factory) as uow:
        uow.evaluation_sets.add(ds)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        restored = uow.evaluation_sets.get(SET_ID)
        assert restored == ds
        assert uow.evaluation_sets.get(SET_ID, 1) == ds
        assert uow.evaluation_sets.list() == [ds]


def test_evaluation_repository_versions(session_factory: SessionFactory) -> None:
    ds1 = make_set(version=1)
    ds2 = make_set(version=2).model_copy(update={"name": "Updated Name"})

    with UnitOfWork(session_factory) as uow:
        uow.evaluation_sets.add(ds1)
        uow.evaluation_sets.add(ds2)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.evaluation_sets.get(SET_ID) == ds2  # Latest
        assert uow.evaluation_sets.get(SET_ID, 1) == ds1
        assert uow.evaluation_sets.list_versions(SET_ID) == [ds1, ds2]


def test_evaluation_repository_immutability(session_factory: SessionFactory) -> None:
    ds = make_set()
    with UnitOfWork(session_factory) as uow:
        uow.evaluation_sets.add(ds)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        with pytest.raises(ImmutableEntityError):
            uow.evaluation_sets.update(ds)
        with pytest.raises(ImmutableEntityError):
            uow.evaluation_sets.delete(SET_ID)


def test_integrity_verification_on_load(session_factory: SessionFactory, engine: Engine) -> None:
    ds = make_set()
    with UnitOfWork(session_factory) as uow:
        uow.evaluation_sets.add(ds)
        uow.commit()

    # Corrupt the hash in the DB
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE evaluation_sets SET content_hash = :content_hash"),
            {"content_hash": "f" * 64},
        )

    with (
        UnitOfWork(session_factory) as uow,
        pytest.raises(ValueError, match="integrity verification"),
    ):
        uow.evaluation_sets.get(SET_ID)


def test_uncommitted_evaluation_set_rolls_back(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        uow.evaluation_sets.add(make_set())

    with UnitOfWork(session_factory) as uow:
        assert uow.evaluation_sets.get(SET_ID) is None


def test_create_schema_adds_only_evaluation_snapshot_table() -> None:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    try:
        old_tables = [
            table
            for table in Base.metadata.sorted_tables
            if table is not EvaluationSetRow.__table__
        ]
        Base.metadata.create_all(database, tables=old_tables)
        with database.begin() as connection:
            create_full_text_schema(connection)
        before = set(inspect(database).get_table_names())
        assert "evaluation_sets" not in before

        create_schema(database)
        create_schema(database)

        after = set(inspect(database).get_table_names())
        assert after - before == {"evaluation_sets"}
        columns = {column["name"] for column in inspect(database).get_columns("evaluation_sets")}
        assert columns == {"id", "version", "name", "created_at", "content_hash", "payload"}
        assert "evaluation_items" not in after
    finally:
        database.dispose()
