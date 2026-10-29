from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from konwledge.domain import Document, DocumentContentType, KnowledgeUnit, Source, SourceKind
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionService,
    TraceableTextSplitter,
    fingerprint_text,
)
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
UNIT_IDS = tuple(UUID(f"30000000-0000-0000-0000-{index:012d}") for index in range(1, 10))
NOW = datetime(2026, 10, 19, 9, 30, tzinfo=UTC)


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


def make_document(content_type: DocumentContentType) -> Document:
    return Document(
        id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator="knowledge.md",
        content_type=content_type,
        checksum=fingerprint_text("content"),
        ingested_at=NOW,
    )


def assert_traceable(content: str, units: tuple[KnowledgeUnit, ...]) -> None:
    for unit in units:
        assert content[unit.start_char : unit.end_char] == unit.text


def test_markdown_splitter_tracks_nested_section_paths_and_offsets() -> None:
    content = "Preamble.\n\n# One\nIntro.\n\n## Child ##\nBody.\n\n### Deep\nMore.\n\n# Two\nEnd.\n"
    ids = iter(UNIT_IDS)
    units = TraceableTextSplitter(id_factory=lambda: next(ids)).split(
        make_document(DocumentContentType.MARKDOWN), content
    )

    assert [unit.heading for unit in units] == [None, "One", "Child", "Deep", "Two"]
    assert [unit.section_path for unit in units] == [
        (),
        ("One",),
        ("One", "Child"),
        ("One", "Child", "Deep"),
        ("Two",),
    ]
    assert [unit.ordinal for unit in units] == list(range(5))
    assert units[0].text == "Preamble.\n\n"
    assert units[2].text == "## Child ##\nBody.\n\n"
    assert_traceable(content, units)


def test_markdown_splitter_keeps_leading_whitespace_with_first_heading() -> None:
    content = "\n\n# Visible\nBody.\n"
    units = TraceableTextSplitter(id_factory=lambda: UNIT_IDS[0]).split(
        make_document(DocumentContentType.MARKDOWN), content
    )

    assert len(units) == 1
    assert units[0].start_char == 0
    assert units[0].text == content
    assert units[0].heading == "Visible"
    assert_traceable(content, units)


def test_markdown_splitter_ignores_headings_inside_fenced_code() -> None:
    content = "# Visible\nBefore.\n\n```md\n# Hidden\n```\n\nAfter.\n"
    units = TraceableTextSplitter(id_factory=lambda: UNIT_IDS[0]).split(
        make_document(DocumentContentType.MARKDOWN), content
    )

    assert len(units) == 1
    assert units[0].heading == "Visible"
    assert units[0].section_path == ("Visible",)
    assert units[0].text == content
    assert_traceable(content, units)


def test_text_splitter_preserves_paragraph_separators_and_offsets() -> None:
    content = "first\n\nsecond\n\n\nthird"
    ids = iter(UNIT_IDS)
    units = TraceableTextSplitter(id_factory=lambda: next(ids)).split(
        make_document(DocumentContentType.TEXT), content
    )

    assert [unit.text for unit in units] == ["first\n\n", "second\n\n\n", "third"]
    assert all(unit.heading is None and unit.section_path == () for unit in units)
    assert_traceable(content, units)


def test_markdown_without_headings_falls_back_to_paragraphs() -> None:
    content = "first paragraph\n\nsecond paragraph"
    ids = iter(UNIT_IDS)
    units = TraceableTextSplitter(id_factory=lambda: next(ids)).split(
        make_document(DocumentContentType.MARKDOWN), content
    )

    assert len(units) == 2
    assert_traceable(content, units)


def test_splitter_returns_no_units_for_blank_content() -> None:
    units = TraceableTextSplitter().split(make_document(DocumentContentType.TEXT), " \n\n\t")
    assert units == ()


def test_knowledge_units_roundtrip_with_traceability_metadata(
    session_factory: SessionFactory,
) -> None:
    document = make_document(DocumentContentType.MARKDOWN)
    content = "# One\nBody.\n\n## Child\nMore.\n"
    ids = iter(UNIT_IDS)
    splitter = TraceableTextSplitter(id_factory=lambda: next(ids))

    with UnitOfWork(session_factory) as uow:
        source = Source(
            id=SOURCE_ID,
            kind=SourceKind.FILE,
            uri="knowledge.md",
            created_at=NOW,
        )
        uow.sources.add(source)
        uow.documents.add(document)
        uow.commit()

    result = KnowledgeUnitService(session_factory, splitter=splitter).ensure_units(
        document, content
    )

    assert result.created is True
    with UnitOfWork(session_factory) as uow:
        restored = tuple(uow.knowledge_units.list_by_document(DOCUMENT_ID))
    assert restored == result.units
    assert [unit.section_path for unit in restored] == [("One",), ("One", "Child")]
    assert_traceable(content, restored)


def test_traceable_ingestion_is_idempotent(tmp_path: Path, session_factory: SessionFactory) -> None:
    path = tmp_path / "knowledge.md"
    content = "# One\r\nBody.\r\n\r\n## Child\r\nMore.\r\n"
    path.write_text(content, encoding="utf-8")
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(
            Source(
                id=SOURCE_ID,
                kind=SourceKind.FILE,
                uri=str(path),
                created_at=NOW,
            )
        )
        uow.commit()

    ids = iter(UNIT_IDS[:2])
    ingestion = FileIngestionService(
        session_factory,
        id_factory=lambda: DOCUMENT_ID,
        clock=lambda: NOW,
    )
    unit_service = KnowledgeUnitService(
        session_factory,
        splitter=TraceableTextSplitter(id_factory=lambda: next(ids)),
    )
    service = TraceableIngestionService(ingestion, unit_service)

    first = service.ingest(SOURCE_ID)
    second = service.ingest(SOURCE_ID)

    assert first.ingestion.created is True
    assert first.units_created is True
    assert second.ingestion.created is False
    assert second.units_created is False
    assert second.units == first.units
    assert_traceable(first.ingestion.content, first.units)
    with UnitOfWork(session_factory) as uow:
        assert tuple(uow.knowledge_units.list_by_document(DOCUMENT_ID)) == first.units
