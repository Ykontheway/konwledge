from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

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

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
UNIT_ID = UUID("30000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("40000000-0000-0000-0000-000000000004")
CITATION_ID = UUID("50000000-0000-0000-0000-000000000005")
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
CHECKSUM = "a" * 64


def assert_json_roundtrip(model: Source | Document | KnowledgeUnit | Revision | Citation) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def make_source(**changes: object) -> Source:
    values: dict[str, object] = {
        "id": SOURCE_ID,
        "kind": SourceKind.URL,
        "uri": "https://example.com/knowledge.md",
        "title": "Knowledge notes",
        "created_at": NOW,
        "metadata": {"owner": "demo"},
    }
    values.update(changes)
    return Source.model_validate(values)


def make_document(**changes: object) -> Document:
    values: dict[str, object] = {
        "id": DOCUMENT_ID,
        "source_id": SOURCE_ID,
        "locator": "knowledge.md",
        "content_type": DocumentContentType.MARKDOWN,
        "checksum": CHECKSUM,
        "ingested_at": NOW,
        "metadata": {"language": "en"},
    }
    values.update(changes)
    return Document.model_validate(values)


def make_unit(**changes: object) -> KnowledgeUnit:
    values: dict[str, object] = {
        "id": UNIT_ID,
        "document_id": DOCUMENT_ID,
        "ordinal": 0,
        "text": "Knowledge remains traceable to its source.",
        "start_char": 0,
        "end_char": 42,
        "status": KnowledgeUnitStatus.ACTIVE,
        "tags": {"RAG", "Traceability"},
    }
    values.update(changes)
    return KnowledgeUnit.model_validate(values)


def make_revision(**changes: object) -> Revision:
    values: dict[str, object] = {
        "id": REVISION_ID,
        "entity_type": RevisionEntityType.KNOWLEDGE_UNIT,
        "entity_id": UNIT_ID,
        "revision": 1,
        "change_type": RevisionChangeType.CREATE,
        "created_at": NOW,
        "author": "fixture",
        "payload": {"status": "active"},
    }
    values.update(changes)
    return Revision.model_validate(values)


def make_citation(**changes: object) -> Citation:
    values: dict[str, object] = {
        "id": CITATION_ID,
        "source_id": SOURCE_ID,
        "document_id": DOCUMENT_ID,
        "knowledge_unit_id": UNIT_ID,
        "quote": "Knowledge remains traceable to its source.",
        "locator": "knowledge.md#traceability",
        "start_char": 0,
        "end_char": 42,
        "confidence": 1.0,
    }
    values.update(changes)
    return Citation.model_validate(values)


@pytest.mark.parametrize(
    "model",
    [
        make_source(),
        make_document(),
        make_unit(),
        make_revision(),
        make_revision(change_type=RevisionChangeType.DELETE, payload={}),
        make_citation(),
    ],
)
def test_domain_models_support_json_roundtrip(
    model: Source | Document | KnowledgeUnit | Revision | Citation,
) -> None:
    assert_json_roundtrip(model)


def test_file_source_supports_local_path() -> None:
    source = make_source(kind=SourceKind.FILE, uri="notes/knowledge.md")
    assert source.uri == "notes/knowledge.md"


def test_knowledge_unit_normalizes_tags() -> None:
    unit = make_unit(tags={" RAG ", "Traceability"})
    assert unit.tags == {"rag", "traceability"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"uri": " "}, "String should have at least 1 character"),
        ({"uri": "ftp://example.com/file"}, "URL sources must use an http or https URI"),
        ({"kind": SourceKind.FILE, "uri": "https://example.com/file"}, "file sources"),
        ({"created_at": datetime(2026, 10, 13)}, "must include timezone"),
        ({"title": " "}, "title must not be empty"),
    ],
)
def test_source_rejects_invalid_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_source(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"locator": " "},
        {"checksum": "abc"},
        {"checksum": "A" * 64},
        {"ingested_at": datetime(2026, 10, 13)},
        {"unexpected": "field"},
    ],
)
def test_document_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_document(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"ordinal": -1},
        {"text": " "},
        {"start_char": -1},
        {"start_char": 10, "end_char": 10},
        {"start_char": 10, "end_char": 9},
        {"tags": {"valid", " "}},
    ],
)
def test_knowledge_unit_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_unit(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 0},
        {"created_at": datetime(2026, 10, 13)},
        {"change_type": RevisionChangeType.CREATE, "payload": {}},
        {"change_type": RevisionChangeType.UPDATE, "payload": {}},
        {"author": " "},
    ],
)
def test_revision_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_revision(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": " "},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"start_char": 0, "end_char": None},
        {"start_char": None, "end_char": 1},
        {"start_char": -1, "end_char": 1},
        {"start_char": 3, "end_char": 3},
        {"locator": " "},
    ],
)
def test_citation_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_citation(**changes)
