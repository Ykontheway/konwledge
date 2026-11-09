from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.storage import (
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.database import SessionFactory
from konwledge.storage.full_text import (
    FullTextRepository,
    create_full_text_schema,
    fts_document,
)
from konwledge.storage.tables import Base

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")

UNIT_CHINESE = UUID("30000000-0000-0000-0000-000000000001")
UNIT_SERVICE = UUID("30000000-0000-0000-0000-000000000002")
UNIT_MIXED = UUID("30000000-0000-0000-0000-000000000003")
UNIT_OPERATORS = UUID("30000000-0000-0000-0000-000000000004")
UNIT_BASE = UUID("30000000-0000-0000-0000-000000000010")
UNIT_ROLLBACK = UUID("30000000-0000-0000-0000-000000000011")
UNIT_FILTER_1 = UUID("30000000-0000-0000-0000-000000000101")
UNIT_FILTER_2 = UUID("30000000-0000-0000-0000-000000000102")
UNIT_FILTER_3 = UUID("30000000-0000-0000-0000-000000000103")
UNIT_LEGACY = UUID("30000000-0000-0000-0000-000000000201")


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


def make_source() -> Source:
    return Source(
        id=SOURCE_ID,
        kind=SourceKind.FILE,
        uri="notes/full-text.md",
        title="Full-text fixtures",
        created_at=NOW,
    )


def make_document() -> Document:
    return Document(
        id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator="full-text.md",
        content_type=DocumentContentType.MARKDOWN,
        checksum="a" * 64,
        ingested_at=NOW,
    )


def make_unit(
    unit_id: UUID,
    *,
    ordinal: int,
    text_value: str,
    status: KnowledgeUnitStatus = KnowledgeUnitStatus.ACTIVE,
) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=unit_id,
        document_id=DOCUMENT_ID,
        ordinal=ordinal,
        text=text_value,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + max(len(text_value), 1),
        heading=f"Section {ordinal}",
        section_path=("Full Text", f"Section {ordinal}"),
        status=status,
        tags={"fts"},
    )


def add_metadata(uow: UnitOfWork) -> None:
    uow.sources.add(make_source())
    uow.documents.add(make_document())


def add_units(uow: UnitOfWork, *units: KnowledgeUnit) -> None:
    add_metadata(uow)
    for unit in units:
        uow.knowledge_units.add(unit)


def search_hits(
    session_factory: SessionFactory,
    query: str,
    *,
    eligible_ids: set[UUID],
    limit: int = 50,
) -> tuple[UUID, ...]:
    with UnitOfWork(session_factory) as uow:
        repository = FullTextRepository(uow.session)
        hits = repository.search(query, eligible_ids=eligible_ids, limit=limit)
        return tuple(hit.unit_id for hit in hits)


def token_text(session_factory: SessionFactory, unit_id: UUID) -> str | None:
    with UnitOfWork(session_factory) as uow:
        return cast(
            str | None,
            uow.session.execute(
                text("SELECT tokens FROM knowledge_units_fts WHERE unit_id = :unit_id"),
                {"unit_id": unit_id.hex},
            ).scalar_one_or_none(),
        )


def test_fts_document_splits_cjk_and_unicode_words() -> None:
    assert fts_document("你好world，naïve café_service") == "你 好 world naïve café_service"


def test_search_supports_chinese_english_mixed_service_names_and_unicode(
    session_factory: SessionFactory,
) -> None:
    chinese = make_unit(UNIT_CHINESE, ordinal=0, text_value="中文检索支持相邻短语。")
    service = make_unit(
        UNIT_SERVICE,
        ordinal=1,
        text_value="API service_v2 keeps café and naïve users happy.",
    )
    mixed = make_unit(
        UNIT_MIXED,
        ordinal=2,
        text_value="混合查询覆盖服务 service-name 与 Unicode。",
    )
    with UnitOfWork(session_factory) as uow:
        add_units(uow, chinese, service, mixed)
        uow.commit()

    eligible = {UNIT_CHINESE, UNIT_SERVICE, UNIT_MIXED}
    assert search_hits(session_factory, "中文检索", eligible_ids=eligible) == (UNIT_CHINESE,)
    assert search_hits(session_factory, "service_v2", eligible_ids=eligible) == (UNIT_SERVICE,)
    assert search_hits(session_factory, "cafe naive", eligible_ids=eligible) == (UNIT_SERVICE,)
    assert search_hits(session_factory, "服务 service", eligible_ids=eligible) == (UNIT_MIXED,)


def test_search_treats_operators_and_injection_like_input_as_literal_terms(
    session_factory: SessionFactory,
) -> None:
    operators = make_unit(
        UNIT_OPERATORS,
        ordinal=0,
        text_value="or near and delete from knowledge_units_fts all stay literal",
    )
    plain = make_unit(UNIT_BASE, ordinal=1, text_value="alpha beta gamma")
    with UnitOfWork(session_factory) as uow:
        add_units(uow, operators, plain)
        uow.commit()

    eligible = {UNIT_OPERATORS, UNIT_BASE}
    assert search_hits(session_factory, "OR NEAR AND", eligible_ids=eligible) == (UNIT_OPERATORS,)
    assert search_hits(
        session_factory,
        '"; DELETE FROM knowledge_units_fts; --',
        eligible_ids=eligible,
    ) == (UNIT_OPERATORS,)
    assert search_hits(session_factory, "alpha", eligible_ids=eligible) == (UNIT_BASE,)


def test_search_rejects_blank_query_and_returns_empty_for_punctuation_only(
    session_factory: SessionFactory,
) -> None:
    base = make_unit(UNIT_BASE, ordinal=0, text_value="punctuation should not match")
    with UnitOfWork(session_factory) as uow:
        add_units(uow, base)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        repository = FullTextRepository(uow.session)
        with pytest.raises(ValueError, match="non-whitespace"):
            repository.search("   ", eligible_ids={UNIT_BASE})
        assert repository.search("!!! ???", eligible_ids={UNIT_BASE}) == ()


def test_triggers_follow_transaction_rollback_and_status_cache_does_not_reindex(
    session_factory: SessionFactory,
) -> None:
    base = make_unit(UNIT_BASE, ordinal=0, text_value="stable alpha text")
    with UnitOfWork(session_factory) as uow:
        add_units(uow, base)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        uow.knowledge_units.add(
            make_unit(UNIT_ROLLBACK, ordinal=1, text_value="rollback insert text")
        )

    assert search_hits(session_factory, "rollback insert", eligible_ids={UNIT_ROLLBACK}) == ()

    with UnitOfWork(session_factory) as uow:
        updated = base.model_copy(update={"text": "mutated beta text", "end_char": 117})
        uow.knowledge_units.update(updated)

    assert search_hits(session_factory, "mutated beta", eligible_ids={UNIT_BASE}) == ()
    assert search_hits(session_factory, "stable alpha", eligible_ids={UNIT_BASE}) == (UNIT_BASE,)

    with UnitOfWork(session_factory) as uow:
        assert uow.knowledge_units.delete(UNIT_BASE) is True

    assert search_hits(session_factory, "stable alpha", eligible_ids={UNIT_BASE}) == (UNIT_BASE,)

    before = token_text(session_factory, UNIT_BASE)
    assert before == "stable alpha text"

    with UnitOfWork(session_factory) as uow:
        changed = uow.knowledge_units.set_status(UNIT_BASE, KnowledgeUnitStatus.STALE)
        assert changed is True
        uow.commit()

    after = token_text(session_factory, UNIT_BASE)
    assert after == before
    assert search_hits(session_factory, "stable alpha", eligible_ids={UNIT_BASE}) == (UNIT_BASE,)


def test_search_filters_eligible_ids_before_limit_and_handles_more_than_999_ids(
    session_factory: SessionFactory,
) -> None:
    units = (
        make_unit(UNIT_FILTER_1, ordinal=0, text_value="filter target"),
        make_unit(UNIT_FILTER_2, ordinal=1, text_value="filter target"),
        make_unit(UNIT_FILTER_3, ordinal=2, text_value="filter target"),
    )
    with UnitOfWork(session_factory) as uow:
        add_units(uow, *units)
        uow.commit()

    all_ids = {UNIT_FILTER_1, UNIT_FILTER_2, UNIT_FILTER_3}
    assert search_hits(session_factory, "filter", eligible_ids=all_ids, limit=3) == (
        UNIT_FILTER_1,
        UNIT_FILTER_2,
        UNIT_FILTER_3,
    )

    eligible = {UNIT_FILTER_3} | {UUID(int=10_000 + value) for value in range(1_000)}
    assert search_hits(session_factory, "filter", eligible_ids=eligible, limit=1) == (
        UNIT_FILTER_3,
    )


def test_create_schema_backfills_legacy_rows_after_reopen_and_rebuild_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-full-text.db"
    legacy_engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(legacy_engine)
    legacy_sessions = create_session_factory(legacy_engine)
    with UnitOfWork(legacy_sessions) as uow:
        add_units(uow, make_unit(UNIT_LEGACY, ordinal=0, text_value="legacy backfill search"))
        uow.commit()
    legacy_engine.dispose()

    reopened_engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    create_schema(reopened_engine)
    create_schema(reopened_engine)
    reopened_sessions = create_session_factory(reopened_engine)

    assert search_hits(
        reopened_sessions,
        "legacy backfill",
        eligible_ids={UNIT_LEGACY},
    ) == (UNIT_LEGACY,)

    with UnitOfWork(reopened_sessions) as uow:
        repository = FullTextRepository(uow.session)
        assert repository.rebuild() == 1
        assert repository.rebuild() == 1
        uow.commit()

    with reopened_engine.begin() as connection:
        count: int = connection.execute(
            text("SELECT COUNT(*) FROM knowledge_units_fts")
        ).scalar_one()
    assert count == 1
    assert search_hits(
        reopened_sessions,
        "legacy backfill",
        eligible_ids={UNIT_LEGACY},
    ) == (UNIT_LEGACY,)
    reopened_engine.dispose()


def test_create_full_text_schema_reports_missing_fts5_clearly() -> None:
    class BrokenConnection:
        def exec_driver_sql(self, statement: str) -> None:
            if statement.startswith("CREATE VIRTUAL TABLE"):
                raise OperationalError(
                    statement,
                    None,
                    sqlite3.OperationalError("no such module: fts5"),
                )

    with pytest.raises(RuntimeError, match="FTS5"):
        create_full_text_schema(cast(Any, BrokenConnection()))
