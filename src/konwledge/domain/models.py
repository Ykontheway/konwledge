"""Validated domain objects shared by ingestion, lifecycle, and evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KonwledgeModel(BaseModel):
    """Base configuration for immutable, forward-compatible domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class SourceKind(StrEnum):
    """Supported origin categories for imported knowledge."""

    URL = "url"
    FILE = "file"
    REPOSITORY = "repository"
    MANUAL = "manual"


class DocumentContentType(StrEnum):
    """Content formats supported by the first ingestion milestone."""

    TEXT = "text"
    MARKDOWN = "markdown"


class KnowledgeUnitStatus(StrEnum):
    """Lifecycle states that determine whether a knowledge unit is publishable."""

    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class RevisionEntityType(StrEnum):
    """Entity types tracked in the revision log."""

    SOURCE = "source"
    DOCUMENT = "document"
    KNOWLEDGE_UNIT = "knowledge_unit"
    CITATION = "citation"


class RevisionChangeType(StrEnum):
    """Supported revision operations."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value


class Source(KonwledgeModel):
    """An origin that can contain one or more imported documents."""

    id: UUID
    kind: SourceKind
    uri: str = Field(min_length=1)
    title: str | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("title must not be empty when provided")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_uri_for_kind(self) -> Self:
        is_web_uri = self.uri.startswith(("http://", "https://"))
        if self.kind is SourceKind.URL and not is_web_uri:
            raise ValueError("URL sources must use an http or https URI")
        if self.kind is SourceKind.FILE and is_web_uri:
            raise ValueError("file sources must use a local file URI or path")
        return self


class Document(KonwledgeModel):
    """A normalized document imported from a source at a point in time."""

    id: UUID
    source_id: UUID
    locator: str = Field(min_length=1)
    content_type: DocumentContentType
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    ingested_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ingested_at")
    @classmethod
    def validate_ingested_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "ingested_at")


class KnowledgeUnit(KonwledgeModel):
    """A traceable text unit that can be indexed and retrieved independently."""

    model_config = ConfigDict(str_strip_whitespace=False)

    id: UUID
    document_id: UUID
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int
    heading: str | None = None
    section_path: tuple[str, ...] = ()
    status: KnowledgeUnitStatus = KnowledgeUnitStatus.ACTIVE
    tags: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace content")
        return value

    @field_validator("heading")
    @classmethod
    def validate_heading(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("heading must not be empty when provided")
        return normalized

    @field_validator("section_path")
    @classmethod
    def normalize_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(part.strip() for part in value)
        if any(not part for part in normalized):
            raise ValueError("section_path entries must not be empty")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: set[str]) -> set[str]:
        normalized = {tag.strip().lower() for tag in value}
        if "" in normalized:
            raise ValueError("tags must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        return self


class Revision(KonwledgeModel):
    """An immutable record of a domain entity change."""

    id: UUID
    entity_type: RevisionEntityType
    entity_id: UUID
    revision: int = Field(ge=1)
    change_type: RevisionChangeType
    created_at: datetime
    author: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("author")
    @classmethod
    def validate_author(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("author must not be empty when provided")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.change_type is not RevisionChangeType.DELETE and not self.payload:
            raise ValueError("create and update revisions require a payload")
        return self


class Citation(KonwledgeModel):
    """A model-ready reference from generated context to an exact source unit."""

    id: UUID
    source_id: UUID
    document_id: UUID
    knowledge_unit_id: UUID
    quote: str = Field(min_length=1)
    locator: str | None = None
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("locator must not be empty when provided")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("start_char and end_char must be provided together")
        if (
            self.start_char is not None
            and self.end_char is not None
            and self.end_char <= self.start_char
        ):
            raise ValueError("end_char must be greater than start_char")
        return self
