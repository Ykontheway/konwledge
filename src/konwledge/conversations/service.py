"""Transactional conversation import and human-review service."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import IntegrityError

from konwledge.conversations.extraction import extract_candidates, verify_candidate
from konwledge.domain import (
    Document,
    DocumentContentType,
    KnowledgeUnit,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.domain.conversations import (
    CandidateStatus,
    Conversation,
    ConversationCandidate,
    ConversationImportResult,
    ConversationReview,
    ReviewDecision,
    StoredConversation,
    dump_conversation_json,
    load_conversation_json,
    validate_conversation_size,
)
from konwledge.lifecycle import initial_revisions, materialize_statuses, publish_document
from konwledge.storage import SessionFactory, UnitOfWork

Clock = Callable[[], datetime]
_MATERIALIZATION_NAMESPACE = uuid5(NAMESPACE_URL, "konwledge:conversation-materialization:v1")


class ConversationNotFoundError(LookupError):
    """The requested immutable conversation version does not exist."""


class ConversationVersionConflictError(ValueError):
    """The same conversation id/version was imported with different content."""


class CandidateNotFoundError(LookupError):
    """The requested stored extraction candidate does not exist."""


class CandidateStateError(ValueError):
    """A terminal review conflicts with a candidate's existing decision."""


def _stable_id(candidate_id: UUID, entity: str) -> UUID:
    return uuid5(_MATERIALIZATION_NAMESPACE, f"{candidate_id}:{entity}")


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must contain non-whitespace content")
    return value


class ConversationService:
    """Import exact conversations and publish only explicitly approved candidates.

    Reviewer names are audit attribution supplied by the caller; this service does
    not authenticate that identity. Candidate decisions are terminal. Importing a
    newer version does not supersede or revoke candidates reviewed in older versions.
    """

    def __init__(self, session_factory: SessionFactory, *, clock: Clock | None = None) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def import_conversation(
        self,
        payload: Conversation | Mapping[str, Any] | str | bytes | bytearray,
    ) -> ConversationImportResult:
        conversation = self._parse(payload)
        validate_conversation_size(conversation)
        with UnitOfWork(self._session_factory) as uow:
            existing = uow.conversations.get(conversation.conversation_id, conversation.version)
            if existing is not None:
                if existing.content_hash != conversation.content_hash:
                    raise ConversationVersionConflictError(
                        "conversation id/version already exists with different content"
                    )
                candidates = tuple(
                    uow.conversation_candidates.list(
                        conversation_id=conversation.conversation_id,
                        version=conversation.version,
                    )
                )
                return ConversationImportResult(
                    stored=existing, candidates=candidates, created=False
                )

            imported_at = self._clock()
            stored = StoredConversation(
                conversation=conversation,
                content_hash=conversation.content_hash,
                imported_at=imported_at,
            )
            candidates = extract_candidates(conversation, created_at=imported_at)
            try:
                uow.conversations.add(stored)
                for candidate in candidates:
                    uow.conversation_candidates.add(candidate)
                uow.commit()
            except IntegrityError:
                uow.rollback()
                winner = uow.conversations.get(conversation.conversation_id, conversation.version)
                if winner is None:
                    raise
                if winner.content_hash != conversation.content_hash:
                    raise ConversationVersionConflictError(
                        "conversation id/version concurrently imported with different content"
                    ) from None
                return ConversationImportResult(
                    stored=winner,
                    candidates=tuple(
                        uow.conversation_candidates.list(
                            conversation_id=conversation.conversation_id,
                            version=conversation.version,
                        )
                    ),
                    created=False,
                )
            return ConversationImportResult(stored=stored, candidates=candidates, created=True)

    @staticmethod
    def _parse(
        payload: Conversation | Mapping[str, Any] | str | bytes | bytearray,
    ) -> Conversation:
        if isinstance(payload, Conversation):
            return payload
        if isinstance(payload, (str, bytes, bytearray)):
            return load_conversation_json(payload)
        return Conversation.model_validate(dict(payload))

    def get_conversation(self, conversation_id: str, version: int) -> StoredConversation:
        with UnitOfWork(self._session_factory) as uow:
            stored = uow.conversations.get(conversation_id, version)
            if stored is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} version {version} was not found"
                )
            return stored

    def list_conversations(self) -> tuple[StoredConversation, ...]:
        with UnitOfWork(self._session_factory) as uow:
            return tuple(uow.conversations.list())

    def conversation_history(self, conversation_id: str) -> tuple[StoredConversation, ...]:
        with UnitOfWork(self._session_factory) as uow:
            return tuple(uow.conversations.list_versions(conversation_id))

    def export_conversation(
        self, conversation_id: str, version: int, *, indent: int | None = 2
    ) -> str:
        return dump_conversation_json(
            self.get_conversation(conversation_id, version).conversation,
            indent=indent,
        )

    def list_candidates(
        self,
        *,
        status: CandidateStatus | str | None = None,
        conversation_id: str | None = None,
        version: int | None = None,
    ) -> tuple[ConversationCandidate, ...]:
        normalized = None if status is None else CandidateStatus(status)
        with UnitOfWork(self._session_factory) as uow:
            return tuple(
                uow.conversation_candidates.list(
                    status=normalized,
                    conversation_id=conversation_id,
                    version=version,
                )
            )

    def get_candidate(self, candidate_id: UUID) -> ConversationCandidate:
        with UnitOfWork(self._session_factory) as uow:
            candidate = uow.conversation_candidates.get(candidate_id)
            if candidate is None:
                raise CandidateNotFoundError(f"candidate {candidate_id} was not found")
            return candidate

    def review_history(self, candidate_id: UUID) -> tuple[ConversationReview, ...]:
        with UnitOfWork(self._session_factory) as uow:
            if uow.conversation_candidates.get(candidate_id) is None:
                raise CandidateNotFoundError(f"candidate {candidate_id} was not found")
            return tuple(uow.conversation_reviews.list_for_candidate(candidate_id))

    def approve(
        self, candidate_id: UUID, reviewer: str, reason: str | None = None
    ) -> ConversationReview:
        _nonblank(reviewer, "reviewer")
        if reason is not None:
            _nonblank(reason, "reason")
        try:
            return self._approve(candidate_id, reviewer, reason)
        except IntegrityError:
            # A concurrent identical approval can win deterministic unique keys.
            return self._resolved_after_conflict(candidate_id, ReviewDecision.APPROVED)

    def reject(self, candidate_id: UUID, reviewer: str, reason: str) -> ConversationReview:
        _nonblank(reviewer, "reviewer")
        _nonblank(reason, "reason")
        try:
            with UnitOfWork(self._session_factory) as uow:
                candidate = self._stored_candidate(uow, candidate_id)
                existing = self._terminal_review(uow, candidate, wanted=ReviewDecision.REJECTED)
                if existing is not None:
                    return existing
                rejected = candidate.model_copy(update={"status": CandidateStatus.REJECTED})
                if not uow.conversation_candidates.finalize(rejected):
                    uow.rollback()
                    return self._resolved_after_conflict(candidate_id, ReviewDecision.REJECTED)
                review = ConversationReview(
                    id=_stable_id(candidate.id, "review"),
                    candidate_id=candidate.id,
                    decision=ReviewDecision.REJECTED,
                    reviewer=reviewer,
                    reason=reason,
                    reviewed_at=self._clock(),
                )
                uow.conversation_reviews.add(review)
                uow.commit()
                return review
        except IntegrityError:
            return self._resolved_after_conflict(candidate_id, ReviewDecision.REJECTED)

    def _approve(self, candidate_id: UUID, reviewer: str, reason: str | None) -> ConversationReview:
        with UnitOfWork(self._session_factory) as uow:
            candidate = self._stored_candidate(uow, candidate_id)
            existing = self._terminal_review(uow, candidate, wanted=ReviewDecision.APPROVED)
            if existing is not None:
                return existing
            stored = uow.conversations.get(
                candidate.conversation_id, candidate.conversation_version
            )
            if stored is None:
                raise ValueError("candidate references a missing conversation version")
            verify_candidate(stored.conversation, candidate)
            now = self._clock()
            source, document, unit = self._materialized_entities(candidate, now)

            # Source, document, exact-span unit, FTS trigger rows, lifecycle revisions,
            # terminal candidate and audit record all flush/commit in this one UoW.
            uow.sources.add(source)
            uow.documents.add(document)
            uow.knowledge_units.add(unit)
            for revision in initial_revisions(
                (unit,),
                document=document,
                id_factory=lambda: _stable_id(candidate.id, "unit-revision"),
                clock=lambda: now,
                next_number=lambda lineage_id: uow.revisions.next_number(
                    RevisionEntityType.KNOWLEDGE_UNIT, lineage_id
                ),
            ):
                uow.revisions.add(revision)
            uow.revisions.add(
                Revision(
                    id=_stable_id(candidate.id, "document-revision"),
                    entity_type=RevisionEntityType.DOCUMENT,
                    entity_id=document.id,
                    revision=1,
                    change_type=RevisionChangeType.CREATE,
                    created_at=now,
                    author=reviewer,
                    payload={
                        "change": "approved_conversation_candidate",
                        "candidate_id": str(candidate.id),
                        "document_checksum": document.checksum,
                    },
                )
            )
            publish_document(
                uow,
                document,
                previous_document_id=None,
                reverted=False,
                id_factory=lambda: _stable_id(candidate.id, "source-publication"),
                created_at=now,
            )
            materialize_statuses(uow, source.id)

            approved = candidate.model_copy(
                update={
                    "status": CandidateStatus.APPROVED,
                    "source_id": source.id,
                    "document_id": document.id,
                    "knowledge_unit_id": unit.id,
                }
            )
            # Revalidate model_copy's update before writing security-sensitive refs.
            approved = ConversationCandidate.model_validate(approved.model_dump())
            if not uow.conversation_candidates.finalize(approved):
                uow.rollback()
                return self._resolved_after_conflict(candidate_id, ReviewDecision.APPROVED)
            review = ConversationReview(
                id=_stable_id(candidate.id, "review"),
                candidate_id=candidate.id,
                decision=ReviewDecision.APPROVED,
                reviewer=reviewer,
                reason=reason,
                reviewed_at=now,
                source_id=source.id,
                document_id=document.id,
                knowledge_unit_id=unit.id,
            )
            uow.conversation_reviews.add(review)
            uow.commit()
            return review

    @staticmethod
    def _stored_candidate(uow: UnitOfWork, candidate_id: UUID) -> ConversationCandidate:
        candidate = uow.conversation_candidates.get(candidate_id)
        if candidate is None:
            raise CandidateNotFoundError(f"candidate {candidate_id} was not found")
        return candidate

    @staticmethod
    def _terminal_review(
        uow: UnitOfWork,
        candidate: ConversationCandidate,
        *,
        wanted: ReviewDecision,
    ) -> ConversationReview | None:
        if candidate.status is CandidateStatus.PENDING:
            return None
        actual = ReviewDecision(candidate.status.value)
        if actual is not wanted:
            raise CandidateStateError(
                f"candidate {candidate.id} is already {actual.value}; decisions are terminal"
            )
        review = uow.conversation_reviews.get_for_candidate(candidate.id)
        if review is None:
            raise ValueError("terminal candidate is missing its review audit record")
        return review

    def _resolved_after_conflict(
        self, candidate_id: UUID, wanted: ReviewDecision
    ) -> ConversationReview:
        with UnitOfWork(self._session_factory) as uow:
            candidate = self._stored_candidate(uow, candidate_id)
            review = self._terminal_review(uow, candidate, wanted=wanted)
            if review is None:
                raise CandidateStateError(
                    f"candidate {candidate_id} could not be finalized due to a transaction conflict"
                )
            return review

    @staticmethod
    def _materialized_entities(
        candidate: ConversationCandidate, now: datetime
    ) -> tuple[Source, Document, KnowledgeUnit]:
        source_id = _stable_id(candidate.id, "source")
        document_id = _stable_id(candidate.id, "document")
        unit_id = _stable_id(candidate.id, "unit")
        conversation_key = quote(candidate.conversation_id, safe="")
        message_key = quote(candidate.message_id, safe="")
        base = f"conversation://{conversation_key}/candidates/{candidate.id}"
        locator = (
            f"{base}/versions/{candidate.conversation_version}/messages/{message_key}"
            f"#{candidate.kind.value}:{candidate.start_char}-{candidate.end_char}"
        )
        provenance: dict[str, Any] = {
            "candidate_id": str(candidate.id),
            "candidate_kind": candidate.kind.value,
            "conversation_hash": candidate.conversation_hash,
            "conversation_id": candidate.conversation_id,
            "conversation_version": candidate.conversation_version,
            "message_id": candidate.message_id,
            "message_ordinal": candidate.message_ordinal,
            "message_role": candidate.role.value,
            "original_start_char": candidate.start_char,
            "original_end_char": candidate.end_char,
        }
        source = Source(
            id=source_id,
            kind=SourceKind.CONVERSATION,
            uri=base,
            title=f"Conversation {candidate.conversation_id} candidate",
            created_at=now,
            metadata=provenance,
        )
        document = Document(
            id=document_id,
            source_id=source.id,
            locator=locator,
            content_type=DocumentContentType.TEXT,
            checksum=hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
            ingested_at=now,
            metadata=provenance,
        )
        unit = KnowledgeUnit(
            id=unit_id,
            document_id=document.id,
            ordinal=0,
            text=candidate.text,
            start_char=0,
            end_char=len(candidate.text),
            tags={"conversation", candidate.kind.value},
            metadata={
                **provenance,
                "locator": locator,
                "materialized_start_char": 0,
                "materialized_end_char": len(candidate.text),
            },
        )
        return source, document, unit
