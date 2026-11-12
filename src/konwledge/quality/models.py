from __future__ import annotations

import math
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from konwledge.domain.models import KonwledgeModel
from konwledge.evaluation.baselines import EvaluationBaseline
from konwledge.evaluation.models import AggregateMetrics


class QualityPolicy(KonwledgeModel):
    """Quality acceptance criteria for retrieval performance and evidence integrity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required_top_k: int = Field(default=5, ge=1)
    min_recall: float = Field(default=0.8, ge=0.0, le=1.0)
    min_mrr: float = Field(default=0.65, ge=0.0, le=1.0)
    min_no_answer_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    min_citation_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    max_stale_hits: int = Field(default=0, ge=0)
    allow_include_stale: bool = Field(default=False, strict=True)
    allow_degraded: bool = Field(default=False, strict=True)

    @field_validator(
        "min_recall",
        "min_mrr",
        "min_no_answer_accuracy",
        "min_citation_coverage",
        mode="before",
    )
    @classmethod
    def validate_finite_scores(cls, value: Any, info: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value


class QualityCheck(KonwledgeModel):
    """One named, auditable quality assertion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    passed: bool
    measured: float | int | bool | None
    required: str = Field(min_length=1)
    detail: str | None = None

    @model_validator(mode="after")
    def validate_measurement(self) -> Self:
        if (
            self.measured is not None
            and isinstance(self.measured, (int, float))
            and not math.isfinite(self.measured)
        ):
            raise ValueError(f"check {self.name!r} measured value must be finite")
        return self


class QualityReport(KonwledgeModel):
    """Audit-ready results of a quality acceptance run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    passed: bool
    baseline: EvaluationBaseline
    metrics: AggregateMetrics
    checks: tuple[QualityCheck, ...] = Field(min_length=1)
    citation_coverage: float | None
    stale_hits: int
    degraded: bool
    violations: tuple[str, ...]

    @field_validator("citation_coverage", mode="before")
    @classmethod
    def validate_citation_coverage(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("citation_coverage must be a number or None")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("citation_coverage must be finite and between 0 and 1")
        return value

    @field_validator("violations")
    @classmethod
    def validate_violations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() for entry in value):
            raise ValueError("violations must not contain blank entries")
        return value

    @model_validator(mode="after")
    def validate_passed_consistency(self) -> Self:
        expected = not self.violations and all(check.passed for check in self.checks)
        if self.passed != expected:
            raise ValueError("passed flag must be consistent with checks and violations")
        return self
