"""Conflict domain models for contradictory knowledge evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from konwledge.domain.models import KnowledgeUnitStatus, KonwledgeModel, _ensure_aware


class ConflictStatus(StrEnum):
    """Review lifecycle of a detected contradiction."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class ConflictEvidence(KonwledgeModel):
    """Exact source-backed fact string that participates in a conflict."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
    )

    source_id: UUID
    document_id: UUID
    unit_id: UUID
    lineage_id: UUID
    source_uri: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int
    section_path: tuple[str, ...] = ()
    status: KnowledgeUnitStatus
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @field_validator("source_uri", "locator", "subject", "predicate", "value")
    @classmethod
    def normalize_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("quote")
    @classmethod
    def validate_quote(cls, value: str) -> str:
        if value == "":
            raise ValueError("quote must not be empty")
        if not value.strip():
            raise ValueError("quote must not be blank")
        return value

    @field_validator("section_path")
    @classmethod
    def normalize_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(part.strip() for part in value)
        if any(not part for part in normalized):
            raise ValueError("section_path entries must not be empty")
        return normalized

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: KnowledgeUnitStatus) -> KnowledgeUnitStatus:
        if value not in {KnowledgeUnitStatus.ACTIVE, KnowledgeUnitStatus.STALE}:
            raise ValueError("status must be active or stale")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        if self.end_char - self.start_char != len(self.quote):
            raise ValueError("quote length must match the character range")
        return self


class Conflict(KonwledgeModel):
    """A detected contradiction between two normalized pieces of evidence."""

    id: UUID
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    left: ConflictEvidence
    right: ConflictEvidence
    status: ConflictStatus = ConflictStatus.PENDING
    detected_at: datetime
    reviewed_at: datetime | None = None
    review_reason: str | None = None
    reviewer: str | None = None

    @field_validator("detected_at")
    @classmethod
    def validate_detected_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "detected_at")

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _ensure_aware(value, "reviewed_at")

    @field_validator("review_reason", "reviewer")
    @classmethod
    def validate_optional_non_blank(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        if self.left.source_id == self.right.source_id:
            raise ValueError("left and right evidence must come from different sources")
        if self.left.subject != self.right.subject:
            raise ValueError("left and right evidence must share the same subject")
        if self.left.predicate != self.right.predicate:
            raise ValueError("left and right evidence must share the same predicate")
        if self.left.value == self.right.value:
            raise ValueError("left and right evidence must disagree on value")
        if self.reviewed_at is not None and self.reviewed_at < self.detected_at:
            raise ValueError("reviewed_at must be greater than or equal to detected_at")
        if self.status is not ConflictStatus.PENDING:
            if self.reviewed_at is None:
                raise ValueError("non-pending conflicts require reviewed_at")
            if self.review_reason is None:
                raise ValueError("non-pending conflicts require review_reason")
        return self
