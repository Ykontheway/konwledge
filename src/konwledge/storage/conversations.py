"""Persistence for immutable conversation versions, candidates, and review audit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from konwledge.domain.conversations import (
    CandidateStatus,
    Conversation,
    ConversationCandidate,
    ConversationReview,
    StoredConversation,
)
from konwledge.storage.repository import ImmutableEntityError
from konwledge.storage.tables import (
    ConversationCandidateRow,
    ConversationReviewRow,
    ConversationRow,
)


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, stored: StoredConversation) -> None:
        conversation = stored.conversation
        self._session.add(
            ConversationRow(
                conversation_id=conversation.conversation_id,
                version=conversation.version,
                schema_version=conversation.schema_version,
                content_hash=stored.content_hash,
                imported_at=stored.imported_at.isoformat(),
                payload=conversation.model_dump(mode="json"),
            )
        )
        self._session.flush()

    def get(self, conversation_id: str, version: int) -> StoredConversation | None:
        row = self._session.get(ConversationRow, (conversation_id, version))
        return None if row is None else self._to_entity(row)

    def list(self) -> Sequence[StoredConversation]:
        rows = self._session.scalars(
            select(ConversationRow).order_by(
                ConversationRow.conversation_id, ConversationRow.version
            )
        ).all()
        return [self._to_entity(row) for row in rows]

    def list_versions(self, conversation_id: str) -> Sequence[StoredConversation]:
        rows = self._session.scalars(
            select(ConversationRow)
            .where(ConversationRow.conversation_id == conversation_id)
            .order_by(ConversationRow.version)
        ).all()
        return [self._to_entity(row) for row in rows]

    def update(self, stored: StoredConversation) -> None:
        raise ImmutableEntityError("conversation versions are immutable")

    def delete(self, conversation_id: str, version: int) -> bool:
        raise ImmutableEntityError("conversation versions are immutable")

    @staticmethod
    def _to_entity(row: ConversationRow) -> StoredConversation:
        conversation = Conversation.model_validate(row.payload)
        return StoredConversation.model_validate(
            {
                "conversation": conversation,
                "content_hash": row.content_hash,
                "imported_at": row.imported_at,
            }
        )


class ConversationCandidateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, candidate: ConversationCandidate) -> None:
        self._session.add(self._to_row(candidate))
        self._session.flush()

    def get(self, candidate_id: UUID) -> ConversationCandidate | None:
        row = self._session.get(ConversationCandidateRow, candidate_id)
        return None if row is None else self._to_entity(row)

    def list(
        self,
        *,
        status: CandidateStatus | None = None,
        conversation_id: str | None = None,
        version: int | None = None,
    ) -> Sequence[ConversationCandidate]:
        statement = select(ConversationCandidateRow)
        if status is not None:
            statement = statement.where(ConversationCandidateRow.status == status.value)
        if conversation_id is not None:
            statement = statement.where(ConversationCandidateRow.conversation_id == conversation_id)
        if version is not None:
            statement = statement.where(ConversationCandidateRow.conversation_version == version)
        rows = self._session.scalars(
            statement.order_by(
                ConversationCandidateRow.created_at,
                ConversationCandidateRow.conversation_id,
                ConversationCandidateRow.conversation_version,
                ConversationCandidateRow.message_ordinal,
                ConversationCandidateRow.start_char,
                ConversationCandidateRow.id,
            )
        ).all()
        return [self._to_entity(row) for row in rows]

    def finalize(self, candidate: ConversationCandidate) -> bool:
        """Atomically replace PENDING with one fully validated terminal value."""

        if candidate.status is CandidateStatus.PENDING:
            raise ValueError("finalize requires a terminal candidate status")
        result = self._session.execute(
            update(ConversationCandidateRow)
            .where(
                ConversationCandidateRow.id == candidate.id,
                ConversationCandidateRow.status == CandidateStatus.PENDING.value,
            )
            .values(
                status=candidate.status.value,
                source_id=candidate.source_id,
                document_id=candidate.document_id,
                knowledge_unit_id=candidate.knowledge_unit_id,
            )
        )
        self._session.flush()
        return cast(CursorResult[Any], result).rowcount == 1

    def update(self, candidate: ConversationCandidate) -> None:
        raise ImmutableEntityError("candidates may only change through a terminal review")

    def delete(self, candidate_id: UUID) -> bool:
        raise ImmutableEntityError("conversation candidates are immutable review evidence")

    @staticmethod
    def _to_row(candidate: ConversationCandidate) -> ConversationCandidateRow:
        return ConversationCandidateRow(
            id=candidate.id,
            conversation_id=candidate.conversation_id,
            conversation_version=candidate.conversation_version,
            conversation_hash=candidate.conversation_hash,
            message_id=candidate.message_id,
            message_ordinal=candidate.message_ordinal,
            role=candidate.role.value,
            kind=candidate.kind.value,
            start_char=candidate.start_char,
            end_char=candidate.end_char,
            text=candidate.text,
            status=candidate.status.value,
            created_at=candidate.created_at.isoformat(),
            source_id=candidate.source_id,
            document_id=candidate.document_id,
            knowledge_unit_id=candidate.knowledge_unit_id,
        )

    @staticmethod
    def _to_entity(row: ConversationCandidateRow) -> ConversationCandidate:
        return ConversationCandidate.model_validate(
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "conversation_version": row.conversation_version,
                "conversation_hash": row.conversation_hash,
                "message_id": row.message_id,
                "message_ordinal": row.message_ordinal,
                "role": row.role,
                "kind": row.kind,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "text": row.text,
                "status": row.status,
                "created_at": row.created_at,
                "source_id": row.source_id,
                "document_id": row.document_id,
                "knowledge_unit_id": row.knowledge_unit_id,
            }
        )


class ConversationReviewRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, review: ConversationReview) -> None:
        self._session.add(
            ConversationReviewRow(
                id=review.id,
                candidate_id=review.candidate_id,
                decision=review.decision.value,
                reviewer=review.reviewer,
                reason=review.reason,
                reviewed_at=review.reviewed_at.isoformat(),
                source_id=review.source_id,
                document_id=review.document_id,
                knowledge_unit_id=review.knowledge_unit_id,
            )
        )
        self._session.flush()

    def get(self, review_id: UUID) -> ConversationReview | None:
        row = self._session.get(ConversationReviewRow, review_id)
        return None if row is None else self._to_entity(row)

    def get_for_candidate(self, candidate_id: UUID) -> ConversationReview | None:
        row = self._session.scalar(
            select(ConversationReviewRow).where(ConversationReviewRow.candidate_id == candidate_id)
        )
        return None if row is None else self._to_entity(row)

    def list(self) -> Sequence[ConversationReview]:
        rows = self._session.scalars(
            select(ConversationReviewRow).order_by(
                ConversationReviewRow.reviewed_at, ConversationReviewRow.id
            )
        ).all()
        return [self._to_entity(row) for row in rows]

    def list_for_candidate(self, candidate_id: UUID) -> Sequence[ConversationReview]:
        review = self.get_for_candidate(candidate_id)
        return [] if review is None else [review]

    def update(self, review: ConversationReview) -> None:
        raise ImmutableEntityError("conversation review history is append-only")

    def delete(self, review_id: UUID) -> bool:
        raise ImmutableEntityError("conversation review history is append-only")

    @staticmethod
    def _to_entity(row: ConversationReviewRow) -> ConversationReview:
        return ConversationReview.model_validate(
            {
                "id": row.id,
                "candidate_id": row.candidate_id,
                "decision": row.decision,
                "reviewer": row.reviewer,
                "reason": row.reason,
                "reviewed_at": row.reviewed_at,
                "source_id": row.source_id,
                "document_id": row.document_id,
                "knowledge_unit_id": row.knowledge_unit_id,
            }
        )
