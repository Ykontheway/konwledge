"""Public, privacy-bounded values for automatic knowledge evaluation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from konwledge.domain.models import KonwledgeModel
from konwledge.evaluation import EvaluationBaseline, GatePolicy
from konwledge.quality import QualityPolicy
from konwledge.retrieval import RetrievalMode


class AutoEvaluationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class AutoEvaluationConfig(KonwledgeModel):
    """The singleton opt-in configuration, pinned to one immutable dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generation: int = Field(strict=True, ge=1)
    enabled: bool = Field(strict=True)
    dataset_id: UUID
    dataset_version: int = Field(strict=True, ge=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_policy: QualityPolicy
    baseline: EvaluationBaseline | None = None
    regression_policy: GatePolicy | None = None
    mode: RetrievalMode = RetrievalMode.LEXICAL
    top_k: int = Field(default=5, strict=True, gt=0)
    candidate_limit: int | None = Field(default=None, strict=True, gt=0)
    rrf_k: int = Field(default=60, strict=True, gt=0)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_pins(self) -> AutoEvaluationConfig:
        if self.candidate_limit is not None and self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        if self.regression_policy is not None and self.baseline is None:
            raise ValueError("regression_policy requires an explicit baseline")
        if self.baseline is not None:
            baseline = self.baseline
            if (
                baseline.dataset.id != self.dataset_id
                or baseline.dataset.version != self.dataset_version
                or baseline.dataset_hash != self.dataset_hash
            ):
                raise ValueError("baseline does not match the pinned evaluation dataset")
            if baseline.degraded:
                raise ValueError("a degraded snapshot cannot be used as a regression baseline")
            expected_limit = (
                max(50, self.top_k) if self.candidate_limit is None else self.candidate_limit
            )
            if (
                baseline.mode is not self.mode
                or baseline.top_k != self.top_k
                or baseline.candidate_limit != expected_limit
                or baseline.rrf_k != self.rrf_k
            ):
                raise ValueError("baseline retrieval settings do not match automatic evaluation")
        return self

    @field_validator("enabled", "dataset_version", "top_k", "rrf_k", mode="before")
    @classmethod
    def validate_strict_types(cls, value: Any, info: Any) -> Any:
        if info.field_name == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be a boolean")
        elif info.field_name in {"dataset_version", "top_k", "rrf_k"} and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ValueError(f"{info.field_name} must be an integer")
        return value

    @field_validator("candidate_limit", mode="before")
    @classmethod
    def validate_optional_int(cls, value: Any) -> Any:
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError("candidate_limit must be an integer or None")
        return value


class AutoEvaluationJob(KonwledgeModel):
    """One immutable evaluation request plus its mutable execution outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: UUID
    parent_job_id: UUID | None = None
    config_generation: int = Field(strict=True, ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: UUID
    dataset_version: int = Field(strict=True, ge=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_revision_ids: tuple[UUID, ...] = ()
    status: AutoEvaluationStatus
    created_at: AwareDatetime
    evaluated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    lease_expires_at: AwareDatetime | None = None
    report: dict[str, Any] | None = None
    error_code: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    recovery_errors: tuple[dict[str, Any], ...] = ()

    @field_validator("trigger_revision_ids")
    @classmethod
    def validate_unique_revisions(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("trigger_revision_ids must be unique")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and (not value or not value.replace("_", "").isalnum()):
            raise ValueError("error_code must be a safe symbolic code")
        return value
