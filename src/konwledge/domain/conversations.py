"""Immutable conversation imports and human-review domain values."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

CONVERSATION_SCHEMA_VERSION: Literal[1] = 1
MAX_CONVERSATION_BYTES = 10 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_MESSAGE_CHARS = 1_000_000
MAX_IDENTIFIER_CHARS = 256
MAX_METADATA_DEPTH = 20
MAX_METADATA_ITEMS = 10_000


class ConversationModel(BaseModel):
    """Strict immutable base that deliberately does not normalize strings."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
    )


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class CandidateKind(StrEnum):
    FACT = "fact"
    DECISION = "decision"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


def _validate_identifier(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must contain non-whitespace content")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{name} must not exceed {MAX_IDENTIFIER_CHARS} characters")
    return value


def _validate_json_tree(value: Any, *, path: str = "metadata", depth: int = 0) -> int:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds maximum nesting depth {MAX_METADATA_DEPTH}")
    if value is None or isinstance(value, (str, bool, int)):
        return 1
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return 1
    if isinstance(value, list):
        count = 1
        for index, item in enumerate(value):
            count += _validate_json_tree(item, path=f"{path}[{index}]", depth=depth + 1)
            if count > MAX_METADATA_ITEMS:
                raise ValueError(f"{path} exceeds {MAX_METADATA_ITEMS} JSON values")
        return count
    if isinstance(value, dict):
        count = 1
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            count += _validate_json_tree(item, path=f"{path}.{key}", depth=depth + 1)
            if count > MAX_METADATA_ITEMS:
                raise ValueError(f"{path} exceeds {MAX_METADATA_ITEMS} JSON values")
        return count
    raise ValueError(f"{path} contains a value that is not portable JSON")


class ConversationMessage(ConversationModel):
    """One original message; ``content`` and its offsets are never normalized."""

    id: str = Field(strict=True, min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    role: ConversationRole
    content: str = Field(strict=True, max_length=MAX_MESSAGE_CHARS)
    timestamp: AwareDatetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def reject_numeric_timestamp(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("timestamp must be a timezone-aware ISO 8601 timestamp")
        return value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "message id")

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_tree(value)
        return value


class Conversation(ConversationModel):
    """Canonical portable JSON conversation schema version 1.0."""

    schema_version: Literal[1] = CONVERSATION_SCHEMA_VERSION
    conversation_id: str = Field(strict=True, min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    version: int = Field(strict=True, ge=1)
    created_at: AwareDatetime | None = None
    messages: tuple[ConversationMessage, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Literal[1]:
        if type(value) is not int or value != CONVERSATION_SCHEMA_VERSION:
            raise ValueError("schema_version must be the integer 1")
        return CONVERSATION_SCHEMA_VERSION

    @field_validator("created_at", mode="before")
    @classmethod
    def reject_numeric_created_at(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("created_at must be a timezone-aware ISO 8601 timestamp")
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return _validate_identifier(value, "conversation_id")

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_tree(value)
        return value

    @model_validator(mode="after")
    def unique_message_ids(self) -> Self:
        ids = [message.id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("message ids must be unique within a conversation version")
        return self

    @property
    def content_hash(self) -> str:
        """SHA-256 of deterministic UTF-8 canonical JSON (not the input formatting)."""

        return hashlib.sha256(canonical_conversation_json(self).encode("utf-8")).hexdigest()


class StoredConversation(ConversationModel):
    conversation: Conversation
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_at: AwareDatetime

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if self.content_hash != self.conversation.content_hash:
            raise ValueError("stored content_hash does not match the conversation")
        return self


class ConversationCandidate(ConversationModel):
    """An exact half-open source-message span awaiting a terminal review."""

    id: UUID
    conversation_id: str
    conversation_version: int = Field(ge=1)
    conversation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_id: str
    message_ordinal: int = Field(ge=0)
    role: ConversationRole
    kind: CandidateKind
    start_char: int = Field(ge=0)
    end_char: int
    text: str = Field(strict=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
    status: CandidateStatus = CandidateStatus.PENDING
    created_at: AwareDatetime
    source_id: UUID | None = None
    document_id: UUID | None = None
    knowledge_unit_id: UUID | None = None

    @model_validator(mode="after")
    def validate_span_and_materialization(self) -> Self:
        if self.end_char <= self.start_char:
            raise ValueError("candidate end_char must be greater than start_char")
        if self.end_char - self.start_char != len(self.text):
            raise ValueError("candidate range must equal len(text)")
        refs = (self.source_id, self.document_id, self.knowledge_unit_id)
        if self.status is CandidateStatus.APPROVED:
            if any(value is None for value in refs):
                raise ValueError("approved candidates require source/document/unit references")
        elif any(value is not None for value in refs):
            raise ValueError("only approved candidates may have materialized references")
        return self


class ConversationReview(ConversationModel):
    """Append-only attribution and result of one terminal candidate review."""

    id: UUID
    candidate_id: UUID
    decision: ReviewDecision
    reviewer: str = Field(strict=True, min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    reason: str | None = Field(default=None, strict=True, max_length=10_000)
    reviewed_at: AwareDatetime
    source_id: UUID | None = None
    document_id: UUID | None = None
    knowledge_unit_id: UUID | None = None

    @field_validator("reviewer")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        return _validate_identifier(value, "reviewer")

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        refs = (self.source_id, self.document_id, self.knowledge_unit_id)
        if self.decision is ReviewDecision.APPROVED:
            if any(value is None for value in refs):
                raise ValueError("approval review requires source/document/unit references")
        elif any(value is not None for value in refs):
            raise ValueError("rejection review must not reference materialized knowledge")
        if self.decision is ReviewDecision.REJECTED and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("rejection requires a non-empty reason")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must contain non-whitespace content when provided")
        return self


class ConversationImportResult(ConversationModel):
    stored: StoredConversation
    candidates: tuple[ConversationCandidate, ...]
    created: bool


def canonical_conversation_json(conversation: Conversation) -> str:
    """Return the stable portable JSON representation used for hashing/export."""

    return json.dumps(
        conversation.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def dump_conversation_json(conversation: Conversation, *, indent: int | None = 2) -> str:
    """Export canonical fields without changing original message content."""

    return json.dumps(
        conversation.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        sort_keys=True,
    )


def load_conversation_json(payload: str | bytes | bytearray) -> Conversation:
    """Load strict JSON, rejecting oversize input, duplicate keys and NaN/Infinity."""

    if isinstance(payload, str):
        size = len(payload.encode("utf-8"))
        text = payload
    else:
        raw = bytes(payload)
        size = len(raw)
        text = raw.decode("utf-8")
    if size > MAX_CONVERSATION_BYTES:
        raise ValueError(f"conversation JSON exceeds {MAX_CONVERSATION_BYTES} bytes")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    value = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("conversation JSON root must be an object")
    return Conversation.model_validate(value)


def validate_conversation_size(conversation: Conversation) -> None:
    """Apply the byte bound to already-decoded/model inputs too."""

    if len(canonical_conversation_json(conversation).encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ValueError(f"conversation JSON exceeds {MAX_CONVERSATION_BYTES} bytes")
