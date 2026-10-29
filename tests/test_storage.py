from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from konwledge.domain import (
    Citation,
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.storage import (
    EntityNotFoundError,
    ImmutableEntityError,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
UNIT_ID = UUID("30000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("40000000-0000-0000-0000-000000000004")
CITATION_ID = UUID("50000000-0000-0000-0000-000000000005")
SECOND_SOURCE_ID = UUID("10000000-0000-0000-0000-000000000011")
SECOND_DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000012")
SECOND_CITATION_ID = UUID("50000000-0000-0000-0000-000000000015")
NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


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
        uri="notes/knowledge.md",
        title="Knowledge notes",
        created_at=NOW,
        metadata={"owner": "demo"},
    )


def make_document() -> Document:
    return Document(
        id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator="knowledge.md",
        content_type=DocumentContentType.MARKDOWN,
        checksum="a" * 64,
        ingested_at=NOW,
        metadata={"language": "en"},
    )


def make_unit() -> KnowledgeUnit:
    return KnowledgeUnit(
        id=UNIT_ID,
        document_id=DOCUMENT_ID,
        ordinal=0,
        text="Knowledge remains traceable to its source.",
        start_char=0,
        end_char=42,
        heading="Why",
        section_path=("Overview", "Why"),
        status=KnowledgeUnitStatus.ACTIVE,
        tags={"rag", "traceability"},
        metadata={"language": "en"},
    )


def make_revision() -> Revision:
    return Revision(
        id=REVISION_ID,
        entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
        entity_id=UNIT_ID,
        revision=1,
        change_type=RevisionChangeType.CREATE,
        created_at=NOW,
        author="fixture",
        payload={"status": "active"},
    )


def make_citation() -> Citation:
    return Citation(
        id=CITATION_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        knowledge_unit_id=UNIT_ID,
        quote="Knowledge remains traceable to its source.",
        locator="knowledge.md#why",
        start_char=0,
        end_char=42,
        confidence=1.0,
        metadata={"rank": 1},
    )


def add_complete_graph(uow: UnitOfWork) -> None:
    uow.sources.add(make_source())
    uow.documents.add(make_document())
    uow.knowledge_units.add(make_unit())
    uow.revisions.add(make_revision())
    uow.citations.add(make_citation())


def test_repositories_roundtrip_all_domain_models(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        add_complete_graph(uow)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.sources.get(SOURCE_ID) == make_source()
        assert uow.documents.get(DOCUMENT_ID) == make_document()
        assert uow.knowledge_units.get(UNIT_ID) == make_unit()
        assert uow.revisions.get(REVISION_ID) == make_revision()
        assert uow.citations.get(CITATION_ID) == make_citation()
        assert uow.sources.list() == [make_source()]


def test_repository_updates_and_deletes_entities(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        add_complete_graph(uow)
        uow.commit()

    changed_source = make_source().model_copy(update={"title": "Updated notes"})
    with UnitOfWork(session_factory) as uow:
        uow.sources.update(changed_source)
        assert uow.citations.delete(CITATION_ID) is True
        assert uow.citations.delete(UUID(int=0)) is False
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        assert uow.sources.get(SOURCE_ID) == changed_source
        assert uow.citations.get(CITATION_ID) is None


def test_repository_rejects_update_for_missing_entity(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow, pytest.raises(EntityNotFoundError):
        uow.sources.update(make_source())


def test_uncommitted_work_is_rolled_back(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())

    with UnitOfWork(session_factory) as uow:
        assert uow.sources.get(SOURCE_ID) is None


def test_exception_rolls_back_entire_transaction(session_factory: SessionFactory) -> None:
    with pytest.raises(RuntimeError, match="stop transaction"), UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())
        uow.documents.add(make_document())
        raise RuntimeError("stop transaction")

    with UnitOfWork(session_factory) as uow:
        assert uow.sources.list() == []
        assert uow.documents.list() == []


def test_foreign_keys_are_enabled(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        with pytest.raises(IntegrityError):
            uow.documents.add(make_document())
        uow.rollback()


def test_unit_of_work_requires_context(session_factory: SessionFactory) -> None:
    uow = UnitOfWork(session_factory)
    with pytest.raises(RuntimeError, match="inside a with block"):
        uow.commit()


def test_repository_update_flushes_constraint_failures(session_factory: SessionFactory) -> None:
    second_document = make_document().model_copy(
        update={"id": SECOND_DOCUMENT_ID, "locator": "second.md"}
    )
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())
        uow.documents.add(make_document())
        uow.documents.add(second_document)
        uow.commit()

    duplicate_document = second_document.model_copy(update={"locator": "knowledge.md"})
    with UnitOfWork(session_factory) as uow:
        with pytest.raises(IntegrityError):
            uow.documents.update(duplicate_document)
        uow.rollback()


def test_repository_delete_flushes_constraint_failures(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())
        uow.documents.add(make_document())
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        with pytest.raises(IntegrityError):
            uow.sources.delete(SOURCE_ID)
        uow.rollback()


def test_citation_rejects_mismatched_source_and_document(
    session_factory: SessionFactory,
) -> None:
    second_source = make_source().model_copy(
        update={"id": SECOND_SOURCE_ID, "uri": "notes/second.md"}
    )
    mismatched_citation = make_citation().model_copy(
        update={"id": SECOND_CITATION_ID, "source_id": SECOND_SOURCE_ID}
    )
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())
        uow.sources.add(second_source)
        uow.documents.add(make_document())
        uow.knowledge_units.add(make_unit())
        with pytest.raises(IntegrityError):
            uow.citations.add(mismatched_citation)
        uow.rollback()


def test_citation_rejects_mismatched_document_and_knowledge_unit(
    session_factory: SessionFactory,
) -> None:
    second_document = make_document().model_copy(
        update={"id": SECOND_DOCUMENT_ID, "locator": "second.md"}
    )
    mismatched_citation = make_citation().model_copy(
        update={"id": SECOND_CITATION_ID, "document_id": SECOND_DOCUMENT_ID}
    )
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(make_source())
        uow.documents.add(make_document())
        uow.documents.add(second_document)
        uow.knowledge_units.add(make_unit())
        with pytest.raises(IntegrityError):
            uow.citations.add(mismatched_citation)
        uow.rollback()


def test_revision_repository_is_append_only(session_factory: SessionFactory) -> None:
    with UnitOfWork(session_factory) as uow:
        with pytest.raises(ImmutableEntityError, match="append-only"):
            uow.revisions.update(make_revision())
        with pytest.raises(ImmutableEntityError, match="append-only"):
            uow.revisions.delete(REVISION_ID)
