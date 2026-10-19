from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from konwledge.domain import Document, DocumentContentType, Source, SourceKind
from konwledge.ingestion import (
    FileIngestionService,
    SourceNotFoundError,
    TextFileLoader,
    UnsupportedFileTypeError,
    UnsupportedSourceKindError,
    fingerprint_text,
    normalize_text,
)
from konwledge.storage import (
    DocumentRepository,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
SECOND_DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000003")
NOW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)


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


def store_source(
    session_factory: SessionFactory, path: Path, *, kind: SourceKind = SourceKind.FILE
) -> Source:
    source = Source(
        id=SOURCE_ID,
        kind=kind,
        uri=str(path),
        title=path.name,
        created_at=NOW,
    )
    with UnitOfWork(session_factory) as uow:
        uow.sources.add(source)
        uow.commit()
    return source


def test_normalize_text_canonicalizes_unicode_and_line_endings() -> None:
    assert normalize_text("Cafe\u0301\r\nnext\rlast\n") == "Café\nnext\nlast\n"


def test_fingerprint_text_uses_utf8_sha256() -> None:
    content = "知识\n"
    assert fingerprint_text(content) == sha256(content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("suffix", "content_type"),
    [
        (".md", DocumentContentType.MARKDOWN),
        (".markdown", DocumentContentType.MARKDOWN),
        (".txt", DocumentContentType.TEXT),
        (".MD", DocumentContentType.MARKDOWN),
        (".TXT", DocumentContentType.TEXT),
    ],
)
def test_loader_maps_supported_extensions(
    tmp_path: Path, suffix: str, content_type: DocumentContentType
) -> None:
    path = tmp_path / f"knowledge{suffix}"
    path.write_text("content", encoding="utf-8")

    loaded = TextFileLoader().load(path)

    assert loaded.content_type is content_type
    assert loaded.content == "content"


def test_loader_removes_utf8_bom_and_normalizes_newlines(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.md"
    path.write_bytes(b"\xef\xbb\xbfline one\r\nline two\r")

    loaded = TextFileLoader().load(path)

    assert loaded.content == "line one\nline two\n"
    assert loaded.checksum == fingerprint_text(loaded.content)


def test_loader_accepts_empty_text_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    loaded = TextFileLoader().load(path)

    assert loaded.content == ""
    assert loaded.checksum == sha256(b"").hexdigest()


def test_loader_rejects_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.pdf"
    path.write_bytes(b"content")

    with pytest.raises(UnsupportedFileTypeError, match="unsupported file extension"):
        TextFileLoader().load(path)


def test_loader_reports_missing_supported_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TextFileLoader().load(tmp_path / "missing.md")


def test_loader_rejects_non_utf8_content(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.txt"
    path.write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        TextFileLoader().load(path)


def test_repeated_ingestion_is_idempotent(tmp_path: Path, session_factory: SessionFactory) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text("# Knowledge\r\n\r\nTraceable.\r\n", encoding="utf-8")
    store_source(session_factory, path)
    ids = iter([DOCUMENT_ID])
    service = FileIngestionService(
        session_factory,
        id_factory=lambda: next(ids),
        clock=lambda: NOW,
    )

    first = service.ingest(SOURCE_ID)
    second = service.ingest(SOURCE_ID)

    assert first.created is True
    assert second.created is False
    assert second.document == first.document
    assert second.content == "# Knowledge\n\nTraceable.\n"
    with UnitOfWork(session_factory) as uow:
        assert uow.documents.list() == [first.document]


def test_changed_content_creates_a_new_document_version(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    path = tmp_path / "knowledge.txt"
    path.write_text("first", encoding="utf-8")
    store_source(session_factory, path)
    ids = iter([DOCUMENT_ID, SECOND_DOCUMENT_ID])
    service = FileIngestionService(
        session_factory,
        id_factory=lambda: next(ids),
        clock=lambda: NOW,
    )

    first = service.ingest(SOURCE_ID)
    path.write_text("second", encoding="utf-8")
    second = service.ingest(SOURCE_ID)

    assert first.document.id == DOCUMENT_ID
    assert second.document.id == SECOND_DOCUMENT_ID
    assert second.created is True
    assert second.document.checksum != first.document.checksum
    with UnitOfWork(session_factory) as uow:
        assert len(uow.documents.list()) == 2


def test_ingestion_requires_an_existing_file_source(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    service = FileIngestionService(session_factory)

    with pytest.raises(SourceNotFoundError, match="was not found"):
        service.ingest(SOURCE_ID)

    path = tmp_path / "knowledge.md"
    path.write_text("content", encoding="utf-8")
    store_source(session_factory, path, kind=SourceKind.MANUAL)
    with pytest.raises(UnsupportedSourceKindError, match="expected file"):
        service.ingest(SOURCE_ID)


def test_concurrent_duplicate_returns_the_winning_document(
    tmp_path: Path,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text("content", encoding="utf-8")
    store_source(session_factory, path)
    winner = Document(
        id=SECOND_DOCUMENT_ID,
        source_id=SOURCE_ID,
        locator=str(path),
        content_type=DocumentContentType.MARKDOWN,
        checksum=fingerprint_text("content"),
        ingested_at=NOW,
    )
    lookup_count = 0

    def fake_lookup(
        _repository: DocumentRepository, *, source_id: UUID, locator: str, checksum: str
    ) -> Document | None:
        nonlocal lookup_count
        lookup_count += 1
        assert (source_id, locator, checksum) == (SOURCE_ID, str(path), winner.checksum)
        return None if lookup_count == 1 else winner

    def fake_add(_repository: DocumentRepository, _document: Document) -> None:
        raise IntegrityError("insert document", {}, Exception("unique constraint"))

    monkeypatch.setattr(DocumentRepository, "get_by_import_identity", fake_lookup)
    monkeypatch.setattr(DocumentRepository, "add", fake_add)

    result = FileIngestionService(
        session_factory,
        id_factory=lambda: DOCUMENT_ID,
        clock=lambda: NOW,
    ).ingest(SOURCE_ID)

    assert result.document == winner
    assert result.created is False
    assert lookup_count == 2


def test_ingestion_reraises_unrelated_integrity_error(
    tmp_path: Path,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text("content", encoding="utf-8")
    store_source(session_factory, path)

    def fake_add(_repository: DocumentRepository, _document: Document) -> None:
        raise IntegrityError("insert document", {}, Exception("other constraint"))

    monkeypatch.setattr(DocumentRepository, "add", fake_add)

    with pytest.raises(IntegrityError, match="other constraint"):
        FileIngestionService(
            session_factory,
            id_factory=lambda: DOCUMENT_ID,
            clock=lambda: NOW,
        ).ingest(SOURCE_ID)
