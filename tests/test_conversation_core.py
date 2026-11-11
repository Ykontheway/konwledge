from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from konwledge.application import KnowledgeLibrary
from konwledge.conversations import (
    CandidateNotFoundError,
    CandidateStateError,
    CandidateStatus,
    ConversationVersionConflictError,
)
from konwledge.domain import SourceKind
from konwledge.storage import (
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


@pytest.fixture
def library() -> Iterator[KnowledgeLibrary]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    try:
        yield KnowledgeLibrary(session_factory, clock=lambda: NOW)
    finally:
        engine.dispose()


def test_conversation_import_and_extraction(library: KnowledgeLibrary) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "conversation_id": "conv-1",
        "version": 1,
        "messages": [
            {"id": "msg-1", "role": "user", "content": "Fact: The project started in 2024.\n"},
            {"id": "msg-2", "role": "assistant", "content": "Decision: Use Python 3.11.\r\n"},
            {"id": "msg-3", "role": "system", "content": "Fact: Hidden secret.\n"},
        ],
    }

    result = library.conversations.import_conversation(payload)
    assert result.created is True
    assert result.stored.conversation.conversation_id == "conv-1"
    assert len(result.candidates) == 2

    # Candidate 1: Fact
    c1 = result.candidates[0]
    assert c1.text == "The project started in 2024."
    assert c1.kind == "fact"
    assert c1.role == "user"

    # Candidate 2: Decision
    c2 = result.candidates[1]
    assert c2.text == "Use Python 3.11."
    assert c2.kind == "decision"
    assert c2.role == "assistant"

    # Idempotency
    result2 = library.conversations.import_conversation(payload)
    assert result2.created is False
    assert result2.stored.content_hash == result.stored.content_hash

    # Conflict detection
    payload_diff = payload.copy()
    payload_diff["messages"] = [payload["messages"][0]]
    with pytest.raises(ConversationVersionConflictError):
        library.conversations.import_conversation(payload_diff)


def test_human_review_approval_lifecycle(library: KnowledgeLibrary) -> None:
    payload = {
        "schema_version": 1,
        "conversation_id": "c1",
        "version": 1,
        "messages": [{"id": "m1", "role": "user", "content": "Fact: Approved content"}],
    }
    imp = library.conversations.import_conversation(payload)
    candidate_id = imp.candidates[0].id

    # Search should be empty before approval
    assert library.search("Approved content").hits == ()

    # Approve
    review = library.conversations.approve(candidate_id, "reviewer-1", reason="Looks good")
    assert review.decision == "approved"
    assert review.reviewer == "reviewer-1"

    # Search should now find it
    search_result = library.search("Approved content")
    assert len(search_result.hits) == 1
    hit = search_result.hits[0]
    assert hit.unit.text == "Approved content"
    assert hit.source.kind == SourceKind.CONVERSATION
    assert "conv-1" not in hit.source.uri  # it uses conversation_id "c1"
    assert "c1" in hit.source.uri

    # Status in candidate record updated
    candidates = library.conversations.list_candidates(status=CandidateStatus.APPROVED)
    assert len(candidates) == 1
    assert candidates[0].id == candidate_id

    # Materialized units preserved across approval
    with UnitOfWork(library._session_factory) as uow:
        assert uow.sources.get(hit.source.id) is not None
        assert uow.documents.get(hit.document.id) is not None
        assert uow.knowledge_units.get(hit.unit.id) is not None

    # Idempotent approval
    review2 = library.conversations.approve(candidate_id, "reviewer-2")
    assert review2.id == review.id
    assert review2.reviewer == "reviewer-1"  # original winner

    # Decision is terminal: cannot reject approved
    with pytest.raises(CandidateStateError):
        library.conversations.reject(candidate_id, "reviewer-3", reason="changed mind")


def test_human_review_rejection(library: KnowledgeLibrary) -> None:
    payload = {
        "schema_version": 1,
        "conversation_id": "c1",
        "version": 1,
        "messages": [{"id": "m1", "role": "user", "content": "Fact: Bad content"}],
    }
    imp = library.conversations.import_conversation(payload)
    candidate_id = imp.candidates[0].id

    # Reject
    review = library.conversations.reject(candidate_id, "reviewer-1", reason="Inaccurate")
    assert review.decision == "rejected"
    assert review.reason == "Inaccurate"

    # Search should be empty
    assert library.search("Bad content").hits == ()

    # Status updated
    candidate = library.conversations.get_candidate(candidate_id)
    assert candidate.status == CandidateStatus.REJECTED

    # Decision is terminal: cannot approve rejected
    with pytest.raises(CandidateStateError):
        library.conversations.approve(candidate_id, "reviewer-2")


def test_transactional_rollback_on_failure(library: KnowledgeLibrary) -> None:
    payload = {
        "schema_version": 1,
        "conversation_id": "c1",
        "version": 1,
        "messages": [{"id": "m1", "role": "user", "content": "Fact: Rollback test"}],
    }
    imp = library.conversations.import_conversation(payload)
    candidate_id = imp.candidates[0].id

    with pytest.raises(CandidateNotFoundError):
        library.conversations.approve(UUID(int=999), "alice")

    assert library.conversations.get_candidate(candidate_id).status is CandidateStatus.PENDING
    with UnitOfWork(library._session_factory) as uow:
        assert len(uow.sources.list()) == 0


def test_extraction_edge_cases(library: KnowledgeLibrary) -> None:
    payload = {
        "schema_version": 1,
        "conversation_id": "c1",
        "version": 1,
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "content": "Fact: \nDecision: \nFact: Valid\nDecision: Valid?",
            },
        ],
    }
    imp = library.conversations.import_conversation(payload)
    # Empty markers and questions should be skipped
    assert len(imp.candidates) == 1
    assert imp.candidates[0].text == "Valid"
    assert imp.candidates[0].kind == "fact"
