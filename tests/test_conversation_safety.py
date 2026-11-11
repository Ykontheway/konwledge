"""U19 safety acceptance tests for immutable conversation ingestion and review."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from konwledge.api.app import create_app
from konwledge.application import KnowledgeLibrary
from konwledge.conversations import (
    CandidateStateError,
    CandidateStatus,
    Conversation,
    ConversationVersionConflictError,
    canonical_conversation_json,
    load_conversation_json,
)
from konwledge.domain import SourceKind
from konwledge.domain.conversations import MAX_IDENTIFIER_CHARS, MAX_MESSAGE_CHARS
from konwledge.embedding import EmbeddingService, EmbeddingVector
from konwledge.indexing import IndexingService, InMemoryVectorIndex
from konwledge.retrieval import RetrievalMode
from konwledge.storage import (
    ImmutableEntityError,
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from konwledge.storage.repository import KnowledgeUnitRepository
from konwledge.storage.tables import Base

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
TABLES = (
    "sources",
    "documents",
    "knowledge_units",
    "revisions",
    "knowledge_units_fts",
    "conversations",
    "conversation_candidates",
    "conversation_reviews",
)


class DeterministicProvider:
    """A real local embedding provider with deterministic nonzero vectors."""

    name = "u19-deterministic"
    model = "constant-v1"
    dimension = 2

    def embed(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        return [(1.0, 0.0) for _ in texts]


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


@pytest.fixture
def library(session_factory: SessionFactory) -> KnowledgeLibrary:
    return KnowledgeLibrary(session_factory, clock=lambda: NOW)


def payload(
    content: str = "Fact: approved safety evidence",
    *,
    conversation_id: str = "u19-conversation",
    version: int = 1,
    role: str = "user",
    message_id: str = "message-1",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "conversation_id": conversation_id,
        "version": version,
        "created_at": "2026-11-11T10:00:00+00:00",
        "messages": [
            {
                "id": message_id,
                "role": role,
                "content": content,
                "timestamp": "2026-11-11T10:01:00Z",
                "metadata": {"channel": "acceptance"},
            }
        ],
        "metadata": {"suite": "U19"},
    }


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
            for table in TABLES
        }


def configured_library(
    session_factory: SessionFactory,
) -> tuple[KnowledgeLibrary, IndexingService]:
    indexing = IndexingService(
        embedding_service=EmbeddingService(DeterministicProvider()),
        vector_index=InMemoryVectorIndex(dimension=2),
    )
    return (
        KnowledgeLibrary(session_factory, clock=lambda: NOW, indexing_service=indexing),
        indexing,
    )


def materialized_unit(library: KnowledgeLibrary, candidate_id: UUID) -> Any:
    candidate = library.conversations.get_candidate(candidate_id)
    assert candidate.knowledge_unit_id is not None
    with UnitOfWork(library._session_factory) as uow:
        unit = uow.knowledge_units.get(candidate.knowledge_unit_id)
    assert unit is not None
    return unit


@pytest.mark.parametrize("location", ["root", "message"])
def test_canonical_json_rejects_extra_fields(location: str) -> None:
    value = payload()
    if location == "root":
        value["unexpected"] = True
    else:
        value["messages"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_conversation_json(json.dumps(value))


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1}',
        (
            '{"schema_version":1,"conversation_id":"c","version":1,'
            '"messages":[{"id":"m","id":"m","role":"user","content":"Fact: x"}]}'
        ),
        (
            '{"schema_version":1,"conversation_id":"c","version":1,'
            '"messages":[{"id":"m","role":"user","content":"Fact: x",'
            '"metadata":{"x":1,"x":2}}]}'
        ),
    ],
)
def test_canonical_json_rejects_duplicate_keys_at_every_depth(raw: str) -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_conversation_json(raw)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_canonical_json_rejects_nonfinite_numbers(constant: str) -> None:
    raw = json.dumps(payload()).replace('"U19"', constant)
    with pytest.raises(ValueError, match="non-finite JSON number"):
        load_conversation_json(raw)


@pytest.mark.parametrize("root", ["null", "[]", "1", "true", '"conversation"', "1.5"])
def test_canonical_json_requires_object_root(root: str) -> None:
    with pytest.raises(ValueError, match="root must be an object"):
        load_conversation_json(root)


@pytest.mark.parametrize("bad_version", [True, False, 1.0, "1", None, 0, -1])
def test_version_is_strict_positive_integer_not_bool(bad_version: Any) -> None:
    value = payload()
    value["version"] = bad_version
    with pytest.raises(ValidationError):
        load_conversation_json(json.dumps(value))


def test_schema_version_bool_is_not_accepted_as_literal_one() -> None:
    value = payload()
    value["schema_version"] = True
    with pytest.raises(ValidationError):
        load_conversation_json(json.dumps(value))


@pytest.mark.parametrize("field", ["created_at", "message_timestamp"])
@pytest.mark.parametrize("value", ["2026-11-11T10:00:00", 1_799_999_999])
def test_timestamps_must_be_timezone_aware(field: str, value: Any) -> None:
    item = payload()
    if field == "created_at":
        item["created_at"] = value
    else:
        item["messages"][0]["timestamp"] = value
    with pytest.raises(ValidationError):
        load_conversation_json(json.dumps(item))


def test_duplicate_message_ids_are_rejected() -> None:
    value = payload()
    value["messages"].append(
        {"id": "message-1", "role": "assistant", "content": "Decision: duplicate"}
    )
    with pytest.raises(ValidationError, match="message ids must be unique"):
        Conversation.model_validate(value)


def test_messages_must_not_be_empty() -> None:
    value = payload()
    value["messages"] = []
    with pytest.raises(ValidationError):
        Conversation.model_validate(value)


@pytest.mark.parametrize("location", ["conversation", "message"])
@pytest.mark.parametrize(
    "bad_metadata",
    [
        {"number": float("nan")},
        {"number": float("inf")},
        {"portable": {1, 2}},
        {"blob": b"not-json"},
    ],
)
def test_metadata_requires_finite_portable_json(
    location: str, bad_metadata: dict[str, Any]
) -> None:
    value = payload()
    if location == "conversation":
        value["metadata"] = bad_metadata
    else:
        value["messages"][0]["metadata"] = bad_metadata
    with pytest.raises(ValidationError):
        Conversation.model_validate(value)


@pytest.mark.parametrize("location", ["conversation", "message"])
def test_metadata_depth_limit_is_enforced(location: str) -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(21):
        child: dict[str, Any] = {}
        cursor["next"] = child
        cursor = child
    value = payload()
    if location == "conversation":
        value["metadata"] = nested
    else:
        value["messages"][0]["metadata"] = nested
    with pytest.raises(ValidationError, match="nesting depth"):
        Conversation.model_validate(value)


@pytest.mark.parametrize("location", ["conversation", "message"])
def test_metadata_item_limit_is_enforced(location: str) -> None:
    oversized = {"items": list(range(10_000))}
    value = payload()
    if location == "conversation":
        value["metadata"] = oversized
    else:
        value["messages"][0]["metadata"] = oversized
    with pytest.raises(ValidationError, match="10000 JSON values"):
        Conversation.model_validate(value)


@pytest.mark.parametrize("field", ["conversation_id", "message_id"])
def test_identifier_length_bound_is_enforced(field: str) -> None:
    value = payload()
    if field == "conversation_id":
        value["conversation_id"] = "x" * (MAX_IDENTIFIER_CHARS + 1)
    else:
        value["messages"][0]["id"] = "x" * (MAX_IDENTIFIER_CHARS + 1)
    with pytest.raises(ValidationError):
        Conversation.model_validate(value)


def test_message_content_length_bound_is_enforced() -> None:
    value = payload()
    value["messages"][0]["content"] = "x" * (MAX_MESSAGE_CHARS + 1)
    with pytest.raises(ValidationError):
        Conversation.model_validate(value)


def test_canonical_json_and_hash_are_key_order_independent() -> None:
    original = Conversation.model_validate(payload("Fact: Unicode 雪 😀\r\n"))
    reordered = load_conversation_json(
        json.dumps(original.model_dump(mode="json"), ensure_ascii=False, sort_keys=False)
    )
    assert canonical_conversation_json(reordered) == canonical_conversation_json(original)
    assert reordered.content_hash == original.content_hash
    assert "雪" in canonical_conversation_json(original)


def test_exact_offsets_preserve_crlf_unicode_bullets_and_ordinals(
    library: KnowledgeLibrary,
) -> None:
    content = (
        "prefix 😀\r\n"
        "  - 事实：  多字节😀事实  \r\n"
        "\t* Decision:\tUse CRLF ✓\t\r\n"
        "+ Fact: Is this?\n"
        "Decision: Final choice"
    )
    result = library.conversations.import_conversation(payload(content))
    assert [candidate.text for candidate in result.candidates] == [
        "多字节😀事实",
        "Use CRLF ✓",
        "Final choice",
    ]
    assert [candidate.kind.value for candidate in result.candidates] == [
        "fact",
        "decision",
        "decision",
    ]
    for candidate in result.candidates:
        assert content[candidate.start_char : candidate.end_char] == candidate.text
        assert candidate.end_char - candidate.start_char == len(candidate.text)
        assert candidate.message_ordinal == 0


@pytest.mark.parametrize("role", ["system", "tool"])
def test_system_and_tool_roles_never_create_candidates(
    library: KnowledgeLibrary, role: str
) -> None:
    result = library.conversations.import_conversation(
        payload("Fact: must remain raw only", conversation_id=f"role-{role}", role=role)
    )
    assert result.candidates == ()


def test_empty_and_question_markers_are_not_candidates(library: KnowledgeLibrary) -> None:
    content = (
        "Fact:\nFact:   \nDecision:\t\r\nFact: Is it true?\n事实：这是问题？\nDecision: declarative"
    )
    result = library.conversations.import_conversation(payload(content))
    assert [(candidate.kind.value, candidate.text) for candidate in result.candidates] == [
        ("decision", "declarative")
    ]


def test_raw_import_has_no_materialized_knowledge_revisions_or_fts(
    engine: Engine, library: KnowledgeLibrary
) -> None:
    result = library.conversations.import_conversation(
        payload("Fact: raw import must be quarantined\nDecision: still pending")
    )
    assert len(result.candidates) == 2
    assert counts(engine) == {
        "sources": 0,
        "documents": 0,
        "knowledge_units": 0,
        "revisions": 0,
        "knowledge_units_fts": 0,
        "conversations": 1,
        "conversation_candidates": 2,
        "conversation_reviews": 0,
    }


def test_pending_approved_rejected_counts_and_reference_isolation(
    library: KnowledgeLibrary,
) -> None:
    imported = library.conversations.import_conversation(
        payload("Fact: pending token\nFact: approved token\nFact: rejected token")
    )
    pending, approved, rejected = imported.candidates
    library.conversations.approve(approved.id, "alice")
    library.conversations.reject(rejected.id, "bob", "not trustworthy")
    assert len(library.conversations.list_candidates(status="pending")) == 1
    assert len(library.conversations.list_candidates(status="approved")) == 1
    assert len(library.conversations.list_candidates(status="rejected")) == 1
    assert library.conversations.get_candidate(pending.id).source_id is None
    assert library.conversations.get_candidate(rejected.id).source_id is None
    assert library.conversations.get_candidate(approved.id).source_id is not None
    assert library.search("pending token").hits == ()
    assert library.search("rejected token").hits == ()
    assert len(library.search("approved token").hits) == 1


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_retrieval_routes_expose_only_approved_materialization(
    session_factory: SessionFactory, mode: RetrievalMode
) -> None:
    library, indexing = configured_library(session_factory)
    rejected = library.conversations.import_conversation(
        payload("Fact: rejected quarantine", conversation_id=f"rejected-{mode.value}")
    ).candidates[0]
    assert library.search("rejected quarantine", mode=mode).hits == ()
    library.conversations.reject(rejected.id, "reviewer", "false")
    assert library.search("rejected quarantine", mode=mode).hits == ()

    approved = library.conversations.import_conversation(
        payload("Fact: approved searchable", conversation_id=f"approved-{mode.value}")
    ).candidates[0]
    assert library.search("approved searchable", mode=mode).hits == ()
    review = library.conversations.approve(approved.id, "reviewer")
    unit = materialized_unit(library, approved.id)
    indexing.index_units([unit])

    result = library.search("approved searchable", mode=mode)
    assert [hit.unit.id for hit in result.hits] == [review.knowledge_unit_id]
    assert all(hit.source.kind is SourceKind.CONVERSATION for hit in result.hits)
    packet = library.build_context("approved searchable", mode=mode)
    assert [block.citation.knowledge_unit_id for block in packet.blocks] == [
        review.knowledge_unit_id
    ]
    assert "approved searchable" in packet.context_text


def test_native_and_openai_context_routes_preserve_approval_isolation(
    library: KnowledgeLibrary,
) -> None:
    imported = library.conversations.import_conversation(
        payload("Fact: api approved token\nFact: api pending token\nFact: api rejected token")
    )
    library.conversations.approve(imported.candidates[0].id, "api-reviewer")
    library.conversations.reject(imported.candidates[2].id, "api-reviewer", "incorrect")
    app = create_app(library)
    as_of = "2030-01-01T00:00:00Z"
    with TestClient(app) as client:
        native = client.post(
            "/v1/context",
            json={"query": "api approved token", "as_of": as_of},
        )
        chat = client.post(
            "/v1/chat/completions",
            json={
                "model": "knowledge-context",
                "messages": [{"role": "user", "content": "api approved token"}],
                "context": {"as_of": as_of},
            },
        )
        pending = client.post("/v1/context", json={"query": "api pending token"})
        rejected = client.post("/v1/context", json={"query": "api rejected token"})
    assert native.status_code == chat.status_code == 200
    wrapped = json.loads(chat.json()["choices"][0]["message"]["content"])
    assert wrapped == native.json()
    assert len(native.json()["blocks"]) == 1
    assert pending.json()["blocks"] == []
    assert rejected.json()["blocks"] == []


def test_approval_materializes_exact_text_and_explicit_provenance(
    library: KnowledgeLibrary,
) -> None:
    raw = "  - Fact:   exact 😀 text with spaces   \r\nignored"
    candidate = library.conversations.import_conversation(
        payload(
            raw,
            conversation_id="id/with space",
            message_id="message/#1",
        )
    ).candidates[0]
    assert candidate.text == "exact 😀 text with spaces"
    review = library.conversations.approve(candidate.id, "provenance-reviewer")
    stored = library.conversations.get_conversation("id/with space", 1)
    assert stored.conversation.messages[0].content == raw
    assert review.source_id is not None
    assert review.document_id is not None
    assert review.knowledge_unit_id is not None
    with UnitOfWork(library._session_factory) as uow:
        source = uow.sources.get(review.source_id)
        document = uow.documents.get(review.document_id)
        unit = uow.knowledge_units.get(review.knowledge_unit_id)
    assert source is not None and document is not None and unit is not None
    expected = {
        "candidate_id": str(candidate.id),
        "candidate_kind": "fact",
        "conversation_hash": stored.content_hash,
        "conversation_id": "id/with space",
        "conversation_version": 1,
        "message_id": "message/#1",
        "message_ordinal": 0,
        "message_role": "user",
        "original_start_char": candidate.start_char,
        "original_end_char": candidate.end_char,
    }
    assert source.kind is SourceKind.CONVERSATION
    assert source.metadata == document.metadata == expected
    assert unit.text == candidate.text == raw[candidate.start_char : candidate.end_char]
    assert unit.start_char == 0 and unit.end_char == len(candidate.text)
    assert unit.metadata | expected == unit.metadata
    assert unit.metadata["materialized_start_char"] == 0
    assert unit.metadata["materialized_end_char"] == len(candidate.text)
    assert "%2F" in source.uri and "%20" in source.uri
    assert "message%2F%231" in document.locator


def test_same_version_idempotence_preserves_terminal_queue_and_changed_payload_fails(
    library: KnowledgeLibrary,
) -> None:
    original = payload("Fact: keep approved\nFact: keep pending")
    first = library.conversations.import_conversation(original)
    review = library.conversations.approve(first.candidates[0].id, "first-reviewer")
    again = library.conversations.import_conversation(original)
    assert not again.created
    assert [candidate.status for candidate in again.candidates] == [
        CandidateStatus.APPROVED,
        CandidateStatus.PENDING,
    ]
    assert library.conversations.review_history(first.candidates[0].id) == (review,)

    changed = payload("Fact: changed payload\nFact: keep pending")
    with pytest.raises(ConversationVersionConflictError):
        library.conversations.import_conversation(changed)
    after = library.conversations.list_candidates(conversation_id="u19-conversation", version=1)
    assert [candidate.status for candidate in after] == [
        CandidateStatus.APPROVED,
        CandidateStatus.PENDING,
    ]
    assert len(library.conversations.review_history(first.candidates[0].id)) == 1


def test_new_version_stays_pending_without_revoking_prior_approved(
    library: KnowledgeLibrary,
) -> None:
    old = library.conversations.import_conversation(payload("Fact: durable old", version=1))
    review = library.conversations.approve(old.candidates[0].id, "reviewer")
    new = library.conversations.import_conversation(payload("Fact: pending new", version=2))
    assert new.candidates[0].status is CandidateStatus.PENDING
    assert (
        library.conversations.get_candidate(old.candidates[0].id).status is CandidateStatus.APPROVED
    )
    assert library.search("durable old").hits[0].unit.id == review.knowledge_unit_id
    assert library.search("pending new").hits == ()
    assert [
        item.conversation.version
        for item in library.conversations.conversation_history("u19-conversation")
    ] == [1, 2]


def test_export_roundtrip_preserves_original_text_and_hash(library: KnowledgeLibrary) -> None:
    original = payload("Fact: snow 雪\r\nDecision: keep\n", message_id="unicode-😀")
    imported = library.conversations.import_conversation(original)
    exported = library.conversations.export_conversation("u19-conversation", 1)
    roundtrip = load_conversation_json(exported)
    assert roundtrip.content_hash == imported.stored.content_hash
    assert roundtrip == imported.stored.conversation
    assert roundtrip.messages[0].content == original["messages"][0]["content"]
    assert canonical_conversation_json(roundtrip).encode("utf-8")


@pytest.mark.parametrize(
    ("operation", "reviewer", "reason"),
    [
        ("approve", "", None),
        ("approve", "   ", None),
        ("approve", "alice", "  "),
        ("reject", "", "reason"),
        ("reject", "   ", "reason"),
        ("reject", "alice", ""),
        ("reject", "alice", " \t "),
    ],
)
def test_reviews_require_valid_actor_and_reason(
    library: KnowledgeLibrary,
    operation: str,
    reviewer: str,
    reason: str | None,
) -> None:
    candidate = library.conversations.import_conversation(payload()).candidates[0]
    method = getattr(library.conversations, operation)
    with pytest.raises(ValueError, match="non-whitespace"):
        method(candidate.id, reviewer, reason)
    assert library.conversations.get_candidate(candidate.id).status is CandidateStatus.PENDING
    assert library.conversations.review_history(candidate.id) == ()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_terminal_decisions_are_idempotent_single_audit_and_inverse_fails(
    library: KnowledgeLibrary, decision: str
) -> None:
    candidate = library.conversations.import_conversation(payload()).candidates[0]
    if decision == "approve":
        first = library.conversations.approve(candidate.id, "winner", "verified")
        second = library.conversations.approve(candidate.id, "loser", "different")
        inverse = partial(library.conversations.reject, candidate.id, "other", "inverse")
    else:
        first = library.conversations.reject(candidate.id, "winner", "incorrect")
        second = library.conversations.reject(candidate.id, "loser", "different")
        inverse = partial(library.conversations.approve, candidate.id, "other")
    assert second == first
    assert second.reviewer == "winner"
    assert library.conversations.review_history(candidate.id) == (first,)
    with pytest.raises(CandidateStateError, match="decisions are terminal"):
        inverse()
    assert library.conversations.review_history(candidate.id) == (first,)


def test_conversation_candidate_and_review_repositories_block_mutation(
    library: KnowledgeLibrary,
) -> None:
    candidate = library.conversations.import_conversation(payload()).candidates[0]
    review = library.conversations.reject(candidate.id, "reviewer", "incorrect")
    stored = library.conversations.get_conversation("u19-conversation", 1)
    terminal = library.conversations.get_candidate(candidate.id)
    with UnitOfWork(library._session_factory) as uow:
        with pytest.raises(ImmutableEntityError):
            uow.conversations.update(stored)
        with pytest.raises(ImmutableEntityError):
            uow.conversations.delete("u19-conversation", 1)
        with pytest.raises(ImmutableEntityError):
            uow.conversation_candidates.update(terminal)
        with pytest.raises(ImmutableEntityError):
            uow.conversation_candidates.delete(candidate.id)
        with pytest.raises(ImmutableEntityError):
            uow.conversation_reviews.update(review)
        with pytest.raises(ImmutableEntityError):
            uow.conversation_reviews.delete(review.id)


def test_review_repository_unique_candidate_prevents_audit_fork(
    library: KnowledgeLibrary,
) -> None:
    candidate = library.conversations.import_conversation(payload()).candidates[0]
    review = library.conversations.reject(candidate.id, "winner", "incorrect")
    duplicate = review.model_copy(update={"id": uuid4(), "reviewer": "forger"})
    with UnitOfWork(library._session_factory) as uow:
        with pytest.raises(IntegrityError):
            uow.conversation_reviews.add(duplicate)
        uow.rollback()
    assert library.conversations.review_history(candidate.id) == (review,)


def test_reopening_sqlite_preserves_raw_history_and_review_states(tmp_path: Path) -> None:
    path = tmp_path / "reopen.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    first_engine = create_sqlite_engine(url)
    create_schema(first_engine)
    first = KnowledgeLibrary(create_session_factory(first_engine), clock=lambda: NOW)
    imported = first.conversations.import_conversation(
        payload("Fact: persisted approved\nFact: persisted rejected\nFact: persisted pending")
    )
    approved_review = first.conversations.approve(imported.candidates[0].id, "alice")
    first.conversations.reject(imported.candidates[1].id, "bob", "incorrect")
    first_engine.dispose()

    second_engine = create_sqlite_engine(url)
    create_schema(second_engine)
    second = KnowledgeLibrary(create_session_factory(second_engine), clock=lambda: NOW)
    try:
        assert [candidate.status for candidate in second.conversations.list_candidates()] == [
            CandidateStatus.APPROVED,
            CandidateStatus.REJECTED,
            CandidateStatus.PENDING,
        ]
        assert second.conversations.review_history(imported.candidates[0].id) == (approved_review,)
        assert len(second.search("persisted approved").hits) == 1
        assert second.search("persisted rejected").hits == ()
        assert second.search("persisted pending").hits == ()
    finally:
        second_engine.dispose()


def test_mid_approval_failure_rolls_back_every_write_then_retry_succeeds(
    engine: Engine,
    library: KnowledgeLibrary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = library.conversations.import_conversation(
        payload("Fact: atomic approval token")
    ).candidates[0]
    original_add = KnowledgeUnitRepository.add

    def fail_after_source_and_document_write(self: KnowledgeUnitRepository, entity: Any) -> None:
        raise RuntimeError("injected after source/document flush")

    monkeypatch.setattr(KnowledgeUnitRepository, "add", fail_after_source_and_document_write)
    with pytest.raises(RuntimeError, match="injected after source/document flush"):
        library.conversations.approve(candidate.id, "atomic-reviewer")
    assert counts(engine) == {
        "sources": 0,
        "documents": 0,
        "knowledge_units": 0,
        "revisions": 0,
        "knowledge_units_fts": 0,
        "conversations": 1,
        "conversation_candidates": 1,
        "conversation_reviews": 0,
    }
    assert library.conversations.get_candidate(candidate.id).status is CandidateStatus.PENDING
    assert library.conversations.review_history(candidate.id) == ()

    monkeypatch.setattr(KnowledgeUnitRepository, "add", original_add)
    review = library.conversations.approve(candidate.id, "atomic-reviewer")
    after = counts(engine)
    assert after["sources"] == after["documents"] == after["knowledge_units"] == 1
    assert after["knowledge_units_fts"] == after["conversation_reviews"] == 1
    assert after["revisions"] >= 2
    assert library.conversations.get_candidate(candidate.id).status is CandidateStatus.APPROVED
    assert review == library.conversations.review_history(candidate.id)[0]
    assert len(library.search("atomic approval token").hits) == 1


def test_two_fresh_engines_concurrent_identical_import_has_no_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-import.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    bootstrap = create_sqlite_engine(url)
    create_schema(bootstrap)
    bootstrap.dispose()
    engines = [create_sqlite_engine(url), create_sqlite_engine(url)]
    libraries = [
        KnowledgeLibrary(create_session_factory(item), clock=lambda: NOW) for item in engines
    ]
    barrier = threading.Barrier(2)

    def run(index: int) -> Any:
        barrier.wait()
        return libraries[index].conversations.import_conversation(payload())

    successes: list[Any] = []
    errors: list[BaseException] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, index) for index in range(2)]
            for future in futures:
                try:
                    successes.append(future.result())
                except BaseException as exc:  # explicit SQLite lock errors are acceptable
                    errors.append(exc)
        assert successes
        assert all(isinstance(error, SQLAlchemyError) for error in errors)
        verifier = KnowledgeLibrary(create_session_factory(engines[0]), clock=lambda: NOW)
        history = verifier.conversations.conversation_history("u19-conversation")
        candidates = verifier.conversations.list_candidates(conversation_id="u19-conversation")
        assert len(history) == len(candidates) == 1
        assert len({candidate.id for candidate in candidates}) == 1
        assert sum(result.created for result in successes) == 1
    finally:
        for item in engines:
            item.dispose()


def test_two_fresh_engines_concurrent_approval_has_one_audit_and_materialization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-approval.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    bootstrap = create_sqlite_engine(url)
    create_schema(bootstrap)
    seeded = KnowledgeLibrary(create_session_factory(bootstrap), clock=lambda: NOW)
    candidate = seeded.conversations.import_conversation(payload()).candidates[0]
    bootstrap.dispose()
    engines = [create_sqlite_engine(url), create_sqlite_engine(url)]
    libraries = [
        KnowledgeLibrary(create_session_factory(item), clock=lambda: NOW) for item in engines
    ]
    barrier = threading.Barrier(2)

    def run(index: int) -> Any:
        barrier.wait()
        return libraries[index].conversations.approve(candidate.id, f"reviewer-{index}")

    successes: list[Any] = []
    errors: list[BaseException] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, index) for index in range(2)]
            for future in futures:
                try:
                    successes.append(future.result())
                except BaseException as exc:  # explicit SQLite lock errors are acceptable
                    errors.append(exc)
        assert successes
        assert all(isinstance(error, (SQLAlchemyError, CandidateStateError)) for error in errors)
        verifier = KnowledgeLibrary(create_session_factory(engines[0]), clock=lambda: NOW)
        audits = verifier.conversations.review_history(candidate.id)
        assert len(audits) == 1
        assert all(review.id == audits[0].id for review in successes)
        final_counts = counts(engines[0])
        assert final_counts["sources"] == 1
        assert final_counts["documents"] == 1
        assert final_counts["knowledge_units"] == 1
        assert final_counts["conversation_reviews"] == 1
        assert final_counts["knowledge_units_fts"] == 1
    finally:
        for item in engines:
            item.dispose()


def test_create_schema_adds_u19_tables_to_preconversation_database(tmp_path: Path) -> None:
    path = tmp_path / "pre-u19.sqlite3"
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{path}")
    conversation_tables = {
        "conversations",
        "conversation_candidates",
        "conversation_reviews",
    }
    old_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in conversation_tables
    ]
    Base.metadata.create_all(engine, tables=old_tables)
    assert not conversation_tables.intersection(inspect(engine).get_table_names())

    create_schema(engine)
    try:
        names = set(inspect(engine).get_table_names())
        assert conversation_tables <= names
        library = KnowledgeLibrary(create_session_factory(engine), clock=lambda: NOW)
        imported = library.conversations.import_conversation(payload())
        assert len(imported.candidates) == 1
        assert library.conversations.get_conversation("u19-conversation", 1) == imported.stored
    finally:
        engine.dispose()
