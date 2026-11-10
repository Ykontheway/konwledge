from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from konwledge.context.citations import (
    CitationEvidenceError,
    citation_from_hit,
    verify_citation,
)
from konwledge.domain import (
    Citation,
    Document,
    DocumentContentType,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Source,
    SourceKind,
)
from konwledge.retrieval.models import RetrievalHit
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
UNIT_ID = UUID("30000000-0000-0000-0000-000000000003")
LINEAGE_ID = UUID("30000000-0000-0000-0000-000000000000")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


def make_source(**changes: Any) -> Source:
    values = {
        "id": SOURCE_ID,
        "kind": SourceKind.FILE,
        "uri": "notes/evidence.md",
        "title": "Evidence Source",
        "created_at": NOW,
        "metadata": {"owner": "test"},
    }
    values.update(changes)
    return Source.model_validate(values)


def make_document(**changes: Any) -> Document:
    values = {
        "id": DOCUMENT_ID,
        "source_id": SOURCE_ID,
        "locator": "evidence.md",
        "content_type": DocumentContentType.MARKDOWN,
        "checksum": "a" * 64,
        "ingested_at": NOW,
    }
    values.update(changes)
    return Document.model_validate(values)


def make_unit(**changes: Any) -> KnowledgeUnit:
    text = "Detailed evidence with emoji 🧪 and \n newlines."
    values = {
        "id": UNIT_ID,
        "lineage_id": LINEAGE_ID,
        "document_id": DOCUMENT_ID,
        "ordinal": 0,
        "text": text,
        "start_char": 100,
        "end_char": 100 + len(text),
        "status": KnowledgeUnitStatus.ACTIVE,
        "section_path": ("Root", "Evidence"),
    }
    values.update(changes)
    return KnowledgeUnit.model_validate(values)


def make_hit(**changes: Any) -> RetrievalHit:
    unit = changes.pop("unit", make_unit())
    doc = changes.pop("document", make_document())
    source = changes.pop("source", make_source())
    values = {
        "unit": unit,
        "document": doc,
        "source": source,
        "status": unit.status,
        "score": 0.95,
    }
    values.update(changes)
    return RetrievalHit.model_validate(values)


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


def test_citation_from_hit_preserves_whitespace_and_unicode() -> None:
    text = " \n Exact quote with 🚀 emoji and leading/trailing spaces. \t "
    unit = make_unit(text=text, start_char=0, end_char=len(text))
    hit = make_hit(unit=unit)

    citation = citation_from_hit(hit)
    assert citation.quote == text
    assert citation.start_char == 0
    assert citation.end_char == len(text)
    assert citation.metadata["source_uri"] == hit.source.uri
    assert citation.metadata["lineage_id"] == str(unit.lineage_id)
    assert citation.metadata["section_path"] == list(unit.section_path)
    assert citation.metadata["document_checksum"] == hit.document.checksum
    assert citation.confidence is None


def test_citation_id_is_stable_and_identity_driven() -> None:
    hit = make_hit()
    c1 = citation_from_hit(hit)

    # Different score/status should NOT change ID
    hit_variant = make_hit(score=0.1, status=KnowledgeUnitStatus.STALE)
    c2 = citation_from_hit(hit_variant)
    assert c1.id == c2.id

    # Changing unit_id SHOULD change ID
    hit_diff_unit = make_hit(unit=make_unit(id=UUID(int=999)))
    c3 = citation_from_hit(hit_diff_unit)
    assert c1.id != c3.id

    # Changing document_id SHOULD change ID while preserving a valid graph
    other_document_id = UUID(int=998)
    hit_diff_document = make_hit(
        document=make_document(id=other_document_id),
        unit=make_unit(document_id=other_document_id),
    )
    assert c1.id != citation_from_hit(hit_diff_document).id

    # Changing quote SHOULD change ID
    text2 = hit.unit.text + "!"
    hit_diff_quote = make_hit(unit=make_unit(text=text2, end_char=hit.unit.start_char + len(text2)))
    c4 = citation_from_hit(hit_diff_quote)
    assert c1.id != c4.id


def test_citation_json_roundtrip() -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)

    dumped = citation.model_dump_json()
    restored = Citation.model_validate_json(dumped)
    assert restored == citation
    # Ensure no extra whitespace was added/removed during serialization
    assert restored.quote == citation.quote


def test_citation_repository_roundtrip(session_factory: SessionFactory) -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)

    with UnitOfWork(session_factory) as uow:
        uow.sources.add(hit.source)
        uow.documents.add(hit.document)
        uow.knowledge_units.add(hit.unit)
        uow.citations.add(citation)
        uow.commit()

    with UnitOfWork(session_factory) as uow:
        restored = uow.citations.get(citation.id)
        assert restored == citation
        assert restored.quote == citation.quote  # Explicit check for whitespace


def test_verify_citation_accepts_valid_match() -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)
    # Success if no exception raised
    verify_citation(citation, hit)


def test_verify_citation_rejects_mismatches() -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)

    # Mismatched quote
    bad_quote = citation.model_copy(update={"quote": "Wrong"})
    with pytest.raises(CitationEvidenceError, match="quote"):
        verify_citation(bad_quote, hit)

    # Mismatched metadata
    bad_meta = citation.model_copy(update={"metadata": {"source_uri": "wrong"}})
    with pytest.raises(CitationEvidenceError, match="source_uri"):
        verify_citation(bad_meta, hit)


def test_verify_citation_supports_old_generic_citations() -> None:
    hit = make_hit()
    # Simulate an old citation without metadata or offsets
    citation = Citation(
        id=UUID(int=8765),
        source_id=hit.source.id,
        document_id=hit.document.id,
        knowledge_unit_id=hit.unit.id,
        quote=hit.unit.text,
        locator=hit.document.locator,
        start_char=None,
        end_char=None,
        metadata={},
    )
    # Should verify successfully (offsets are optional, metadata anchors missing is OK)
    verify_citation(citation, hit)


def test_validate_hit_evidence_checks_relations() -> None:
    # Mismatched document-source relation
    bad_doc_source = make_hit(document=make_document(source_id=UUID(int=555)))
    with pytest.raises(CitationEvidenceError, match="source_id"):
        citation_from_hit(bad_doc_source)

    # Mismatched unit-document relation
    bad_unit_doc = make_hit(unit=make_unit(document_id=UUID(int=666)))
    with pytest.raises(CitationEvidenceError, match="document_id"):
        citation_from_hit(bad_unit_doc)


def test_validate_hit_evidence_checks_offsets() -> None:
    # Offset length mismatch
    bad_offset = make_hit(unit=make_unit(start_char=0, end_char=10))  # Text is longer
    with pytest.raises(CitationEvidenceError, match="offset range"):
        citation_from_hit(bad_offset)


def test_citation_model_validation() -> None:
    # Blank quote rejected
    with pytest.raises(ValidationError, match="quote"):
        Citation(
            id=UUID(int=1),
            source_id=SOURCE_ID,
            document_id=DOCUMENT_ID,
            knowledge_unit_id=UNIT_ID,
            quote="   ",
        )

    # Empty locator (after strip) rejected
    with pytest.raises(ValidationError, match="locator"):
        Citation.model_validate(
            {
                "id": UUID(int=1),
                "source_id": SOURCE_ID,
                "document_id": DOCUMENT_ID,
                "knowledge_unit_id": UNIT_ID,
                "quote": "Valid",
                "locator": "  ",
            }
        )

    # Locator is stripped
    c = Citation.model_validate(
        {
            "id": UUID(int=1),
            "source_id": SOURCE_ID,
            "document_id": DOCUMENT_ID,
            "knowledge_unit_id": UNIT_ID,
            "quote": "Valid",
            "locator": "  stripped.md  ",
        }
    )
    assert c.locator == "stripped.md"


def test_citation_confidence_is_none_by_default() -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)
    assert citation.confidence is None

    # Manual creation also stays None if not provided
    c = Citation(
        id=UUID(int=1),
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        knowledge_unit_id=UNIT_ID,
        quote="Test",
    )
    assert c.confidence is None


def test_citation_id_matches_uuid5_of_canonical_identity_json() -> None:
    text = "  exact\nquote with spaces\t"
    unit = make_unit(text=text, start_char=7, end_char=7 + len(text))
    hit = make_hit(unit=unit)

    citation = citation_from_hit(hit)

    identity = {
        "document_id": str(hit.document.id),
        "end": unit.end_char,
        "sha256quote": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_id": str(hit.source.id),
        "start": unit.start_char,
        "unit_id": str(unit.id),
    }
    canonical_json = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    namespace = uuid5(NAMESPACE_URL, "konwledge:context-citation:v1")

    assert citation.id == uuid5(namespace, canonical_json)


def test_verify_citation_rejects_offset_mismatch_when_offsets_present() -> None:
    hit = make_hit()
    citation = citation_from_hit(hit)
    assert citation.start_char is not None and citation.end_char is not None
    shifted = citation.model_copy(
        update={"start_char": citation.start_char + 1, "end_char": citation.end_char + 1}
    )

    with pytest.raises(CitationEvidenceError, match="start_char"):
        verify_citation(shifted, hit)
