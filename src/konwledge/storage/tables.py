"""SQLAlchemy table mappings for knowledge metadata."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all metadata tables."""


class SourceRow(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("kind", "uri"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "locator", "checksum"),
        UniqueConstraint("id", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class KnowledgeUnitRow(Base):
    __tablename__ = "knowledge_units"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        UniqueConstraint("id", "document_id"),
        CheckConstraint("ordinal >= 0"),
        CheckConstraint("start_char >= 0"),
        CheckConstraint("end_char > start_char"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    # Nullable so databases created before U09 can be upgraded in place; the
    # repository treats a missing lineage as "the unit starts its own lineage".
    lineage_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class RevisionRow(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "revision"),
        CheckConstraint("revision >= 1"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CitationRow(Base):
    __tablename__ = "citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "source_id"],
            ["documents.id", "documents.source_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["knowledge_unit_id", "document_id"],
            ["knowledge_units.id", "knowledge_units.document_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("start_char IS NULL OR start_char >= 0"),
        CheckConstraint(
            "(start_char IS NULL AND end_char IS NULL) OR "
            "(start_char IS NOT NULL AND end_char > start_char)"
        ),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    knowledge_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str | None] = mapped_column(Text)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class ConflictRow(Base):
    __tablename__ = "conflicts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["left_document_id", "left_source_id"],
            ["documents.id", "documents.source_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["right_document_id", "right_source_id"],
            ["documents.id", "documents.source_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["left_unit_id", "left_document_id"],
            ["knowledge_units.id", "knowledge_units.document_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["right_unit_id", "right_document_id"],
            ["knowledge_units.id", "knowledge_units.document_id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    detected_at: Mapped[str] = mapped_column(String(40), nullable=False)
    left_source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    left_document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    left_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    right_source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    right_document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    right_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class EvaluationSetRow(Base):
    """One complete immutable evaluation dataset version."""

    __tablename__ = "evaluation_sets"
    __table_args__ = (
        CheckConstraint("version >= 1"),
        CheckConstraint("length(content_hash) = 64"),
        CheckConstraint("content_hash NOT GLOB '*[^0-9a-f]*'"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AutoEvaluationConfigRow(Base):
    """Singleton, explicitly enabled automatic quality configuration."""

    __tablename__ = "auto_evaluation_config"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("generation >= 1"),
        CheckConstraint("dataset_version >= 1"),
        CheckConstraint("length(dataset_hash) = 64"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AutoEvaluationJobRow(Base):
    """Durable outbox item and privacy-bounded execution audit."""

    __tablename__ = "auto_evaluation_jobs"
    __table_args__ = (
        CheckConstraint("config_generation >= 1"),
        CheckConstraint("dataset_version >= 1"),
        CheckConstraint("length(config_fingerprint) = 64"),
        CheckConstraint("length(dataset_hash) = 64"),
        CheckConstraint("length(knowledge_fingerprint) = 64"),
        Index("ix_auto_evaluation_jobs_queue", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    parent_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("auto_evaluation_jobs.id", ondelete="RESTRICT"), index=True
    )
    trigger_key: Mapped[str | None] = mapped_column(String(129), unique=True)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_revision_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    evaluated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[str | None] = mapped_column(String(40))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    recovery_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )


class DatabaseSchemaSnapshotRow(Base):
    """One append-only schema capture; repeated historical content is allowed."""

    __tablename__ = "database_schema_snapshots"
    __table_args__ = (
        CheckConstraint("version >= 1"),
        CheckConstraint("length(content_hash) = 64"),
        UniqueConstraint("source_id", "version"),
    )

    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column("payload", JSON, nullable=False)
    captured_at: Mapped[str] = mapped_column(String(40), nullable=False)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class ConversationRow(Base):
    """One immutable imported conversation version."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("version >= 1"),
        CheckConstraint("length(content_hash) = 64"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ConversationCandidateRow(Base):
    """Extracted review item; no knowledge graph references exist before approval."""

    __tablename__ = "conversation_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "conversation_version"],
            ["conversations.conversation_id", "conversations.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("conversation_version >= 1"),
        CheckConstraint("message_ordinal >= 0"),
        CheckConstraint("start_char >= 0"),
        CheckConstraint("end_char > start_char"),
        CheckConstraint(
            "(status = 'approved' AND source_id IS NOT NULL "
            "AND document_id IS NOT NULL AND knowledge_unit_id IS NOT NULL) OR "
            "(status != 'approved' AND source_id IS NULL "
            "AND document_id IS NULL AND knowledge_unit_id IS NULL)"
        ),
        Index("ix_conversation_candidates_queue", "status", "created_at", "id"),
        Index(
            "ix_conversation_candidates_conversation",
            "conversation_id",
            "conversation_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )
    knowledge_unit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_units.id", ondelete="RESTRICT")
    )


class ConversationReviewRow(Base):
    """Append-only terminal review audit record (one per candidate)."""

    __tablename__ = "conversation_reviews"
    __table_args__ = (
        CheckConstraint(
            "(decision = 'approved' AND source_id IS NOT NULL "
            "AND document_id IS NOT NULL AND knowledge_unit_id IS NOT NULL) OR "
            "(decision = 'rejected' AND source_id IS NULL "
            "AND document_id IS NULL AND knowledge_unit_id IS NULL)"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversation_candidates.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )
    knowledge_unit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_units.id", ondelete="RESTRICT")
    )
