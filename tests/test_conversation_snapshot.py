"""U19 compatibility checks for the deliberately frozen snapshot V1 contract."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from konwledge.application import KnowledgeLibrary
from konwledge.conversations import CandidateStatus
from konwledge.demo import build_demo, export_demo
from konwledge.domain import SourceKind as DomainSourceKind
from konwledge.snapshot import dump_schema, dump_snapshot, export_snapshot
from konwledge.snapshot.v1_contract import SourceKind as SnapshotSourceKind
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

REPO = Path(__file__).resolve().parents[1]
GOLDEN_SNAPSHOT = REPO / "demo" / "snapshot.json"
GOLDEN_SCHEMA = REPO / "demo" / "snapshot.schema.json"
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
UNSUPPORTED_CONVERSATION = (
    "snapshot schema version 1 does not support conversation sources; "
    "use context API or await versioned snapshot extension"
)


@contextmanager
def library_for(database: Path) -> Iterator[tuple[KnowledgeLibrary, SessionFactory]]:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    sessions = create_session_factory(engine)
    try:
        yield KnowledgeLibrary(sessions, clock=lambda: NOW), sessions
    finally:
        engine.dispose()


def conversation_payload(content: str, *, conversation_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "conversation_id": conversation_id,
        "version": 1,
        "messages": [{"id": "message-1", "role": "user", "content": content}],
    }


def stored_state(sessions: SessionFactory) -> tuple[tuple[Any, ...], ...]:
    with UnitOfWork(sessions) as uow:
        return (
            tuple(uow.sources.list()),
            tuple(uow.documents.list()),
            tuple(uow.knowledge_units.list()),
            tuple(uow.revisions.list()),
            tuple(uow.conversations.list()),
            tuple(uow.conversation_candidates.list()),
            tuple(uow.conversation_reviews.list()),
        )


def test_snapshot_v1_schema_and_demo_goldens_remain_byte_identical(tmp_path: Path) -> None:
    schema_text = dump_schema()
    schema = json.loads(schema_text)

    assert schema_text.encode("utf-8") == GOLDEN_SCHEMA.read_bytes()
    assert schema["$defs"]["SourceKind"] == {
        "description": "Supported origin categories for imported knowledge.",
        "enum": ["url", "file", "repository", "manual"],
        "title": "SourceKind",
        "type": "string",
    }
    assert [kind.value for kind in SnapshotSourceKind] == [
        "url",
        "file",
        "repository",
        "manual",
    ]
    assert SnapshotSourceKind.__module__ == "konwledge.snapshot.v1_contract"
    assert DomainSourceKind.__module__ == "konwledge.domain.models"

    demo = build_demo(tmp_path / "demo")
    snapshot = export_demo(demo.workspace)
    assert dump_snapshot(snapshot).encode("utf-8") == GOLDEN_SNAPSHOT.read_bytes()
    assert all(type(source.kind) is SnapshotSourceKind for source in snapshot.sources)


def test_pending_and_rejected_conversations_leave_demo_export_unchanged(tmp_path: Path) -> None:
    demo = build_demo(tmp_path / "demo")
    before = dump_snapshot(export_demo(demo.workspace))

    with library_for(demo.database) as (library, sessions):
        pending = library.conversations.import_conversation(
            conversation_payload("Fact: pending only", conversation_id="pending")
        ).candidates[0]
        rejected = library.conversations.import_conversation(
            conversation_payload("Fact: rejected only", conversation_id="rejected")
        ).candidates[0]
        library.conversations.reject(rejected.id, "reviewer", "not approved")

        assert library.conversations.get_candidate(pending.id).status is CandidateStatus.PENDING
        assert library.conversations.get_candidate(rejected.id).status is CandidateStatus.REJECTED
        with UnitOfWork(sessions) as uow:
            assert all(
                source.kind is not DomainSourceKind.CONVERSATION for source in uow.sources.list()
            )

    after = dump_snapshot(export_demo(demo.workspace))
    assert after == before
    assert after.encode("utf-8") == GOLDEN_SNAPSHOT.read_bytes()


def test_approved_conversation_fails_closed_without_output_or_mutation(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    output = tmp_path / "snapshot.json"

    with library_for(database) as (library, sessions):
        candidate = library.conversations.import_conversation(
            conversation_payload("Fact: approved provenance", conversation_id="approved")
        ).candidates[0]
        review = library.conversations.approve(candidate.id, "reviewer", "verified")
        before = stored_state(sessions)

        with pytest.raises(ValueError) as raised:
            snapshot = export_snapshot(sessions)
            output.write_text(dump_snapshot(snapshot), encoding="utf-8")

        assert str(raised.value) == UNSUPPORTED_CONVERSATION
        assert not output.exists()
        assert stored_state(sessions) == before
        assert review.source_id is not None
        with UnitOfWork(sessions) as uow:
            source = uow.sources.get(review.source_id)
        assert source is not None
        assert source.kind is DomainSourceKind.CONVERSATION
        assert source.kind.value == "conversation"
