"""管理读取白名单契约: 不承载任意报告、源定位符或可变容器。"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from konwledge.auto_evaluation.models import AutoEvaluationStatus
from konwledge.domain.models import (
    DocumentContentType,
    KnowledgeUnitStatus,
    RevisionChangeType,
    RevisionEntityType,
)
from konwledge.lifecycle.diff import UnitChangeKind

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
SafeNumber = Annotated[float, Field(allow_inf_nan=False)]
CheckName = Literal[
    "top_k_contract",
    "recall_at_k",
    "mean_reciprocal_rank",
    "no_answer_accuracy",
    "retrieval_evidence_citation_coverage",
    "stale_or_retired_hits",
    "stale_isolation_configuration",
    "retrieval_degradation",
    "regression_gate",
]
SafeEvent = Literal[
    "added",
    "modified",
    "moved",
    "unchanged",
    "removed",
    "published",
    "stale",
    "refreshed",
    "freshness_policy",
    "approved_conversation_candidate",
    "detected",
    "reviewed",
]
JobErrorCode = Literal[
    "LEASE_EXPIRED",
    "QUALITY_GATE_FAILED",
    "INVALID_EVALUATION",
    "EVALUATION_ERROR",
    "CONFIG_MISSING",
    "INTEGRITY_MISMATCH",
    "CONFIG_DISABLED",
    "CONFIG_CHANGED",
    "DATASET_PIN_MISMATCH",
    "KNOWLEDGE_CHANGED",
    "UNKNOWN_ERROR",
]


class ManagementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class PageQuery(ManagementModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)


class JobsQuery(PageQuery):
    status: AutoEvaluationStatus | None = None
    dataset_id: UUID | None = None
    dataset_version: int | None = Field(default=None, ge=1)
    parent_job_id: UUID | None = None


class DiffQuery(PageQuery):
    old_document_id: UUID


class ResourceLink(ManagementModel):
    kind: Literal["job", "revision", "document", "unit", "lineage", "evidence"]
    id: UUID
    href: str = Field(pattern=r"^/v1/management/(jobs|revisions|documents|units|lineages)/")


class SafeMetrics(ManagementModel):
    case_count: int = Field(ge=0)
    answerable_count: int = Field(ge=0)
    no_answer_count: int = Field(ge=0)
    mean_recall: Score | None
    mean_precision: Score | None
    mean_reciprocal_rank: Score | None
    mean_ndcg: Score | None
    hit_rate: Score | None
    no_answer_accuracy: Score | None


class SafeCheck(ManagementModel):
    name: CheckName
    passed: bool = Field(strict=True)
    actual: bool | int | SafeNumber | None
    # 数值比较表达式或既有有限枚举, 不允许任意 required/detail 字符串。
    threshold: str | None = Field(
        default=None,
        pattern=(
            r"^(?:(?:>=|<=|==)[0-9]+(?:\.[0-9]+)?|allowed|false|pass|"
            r"0 include_stale cases|compatible healthy baseline|"
            r"baseline when regression policy is supplied)$"
        ),
        max_length=64,
    )


class SafeQualityReport(ManagementModel):
    passed: bool = Field(strict=True)
    metrics: SafeMetrics
    citation_coverage: Score | None
    stale_hits: int = Field(ge=0)
    degraded: bool = Field(strict=True)
    checks: tuple[SafeCheck, ...]


class JobDTO(ManagementModel):
    id: UUID
    parent_job_id: UUID | None
    status: AutoEvaluationStatus
    config_generation: int = Field(ge=1)
    config_fingerprint: Hash
    dataset_id: UUID
    dataset_version: int = Field(ge=1)
    dataset_hash: Hash
    knowledge_fingerprint: Hash
    created_at: AwareDatetime
    evaluated_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    attempt_count: int = Field(ge=0)
    error_code: JobErrorCode | None
    report: SafeQualityReport | None
    trigger_revision_ids: tuple[UUID, ...]
    links: tuple[ResourceLink, ...]


class RevisionNavigation(ManagementModel):
    event: SafeEvent | None = None
    document_id: UUID | None = None
    previous_document_id: UUID | None = None
    unit_id: UUID | None = None
    previous_unit_id: UUID | None = None
    document_checksum: Hash | None = None
    content_hash: Hash | None = None
    previous_content_hash: Hash | None = None
    reverted: bool | None = Field(default=None, strict=True)


class RevisionDTO(ManagementModel):
    id: UUID
    entity_type: RevisionEntityType
    entity_id: UUID
    revision: int = Field(ge=1)
    change_type: RevisionChangeType
    created_at: AwareDatetime
    navigation: RevisionNavigation
    affected_document_ids: tuple[UUID, ...]
    affected_unit_ids: tuple[UUID, ...]
    affected_lineage_ids: tuple[UUID, ...]
    links: tuple[ResourceLink, ...]


class UnitDTO(ManagementModel):
    id: UUID
    source_id: UUID
    document_id: UUID
    lineage_id: UUID
    ordinal: int = Field(ge=0)
    status: KnowledgeUnitStatus  # 已持久化缓存值, 不运行状态物化/回填。
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    content_hash: Hash
    links: tuple[ResourceLink, ...]


T = TypeVar("T")


class Page(ManagementModel, Generic[T]):
    items: tuple[T, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=100_000)


class LineageDTO(ManagementModel):
    id: UUID
    versions: Page[UnitDTO]


class DocumentDTO(ManagementModel):
    id: UUID
    source_id: UUID
    content_type: DocumentContentType
    checksum: Hash
    ingested_at: AwareDatetime
    previous_document_id: UUID | None
    next_document_id: UUID | None
    units: Page[UnitDTO]
    links: tuple[ResourceLink, ...]


class DiffItem(ManagementModel):
    kind: UnitChangeKind
    lineage_id: UUID
    old: UnitDTO | None
    new: UnitDTO | None


class DiffCounts(ManagementModel):
    added: int = Field(ge=0)
    modified: int = Field(ge=0)
    moved: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    removed: int = Field(ge=0)


class DocumentDiffDTO(ManagementModel):
    source_id: UUID
    old_document_id: UUID
    new_document_id: UUID
    counts: DiffCounts
    changes: Page[DiffItem]
    links: tuple[ResourceLink, ...]


class EvidenceDTO(ManagementModel):
    source_id: UUID
    document_id: UUID
    unit_id: UUID
    lineage_id: UUID
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text: str = Field(min_length=1)  # 唯一显式授权返回正文的管理响应。
    content_hash: Hash
    document_checksum: Hash
    revision_ids: tuple[UUID, ...]
    links: tuple[ResourceLink, ...]
