"""Immutable public values for lifecycle-aware retrieval."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from konwledge.domain import Document, KnowledgeUnit, KnowledgeUnitStatus, Source
from konwledge.domain.models import KonwledgeModel


class RetrievalMode(StrEnum):
    """Supported retrieval routes."""

    LEXICAL = "lexical"
    VECTOR = "vector"
    HYBRID = "hybrid"


class RetrievalHit(KonwledgeModel):
    """Metadata-backed evidence, with RRF and unmodified per-route scores."""

    unit: KnowledgeUnit
    document: Document
    source: Source
    status: KnowledgeUnitStatus
    stale_reason: str | None = None
    score: float = Field(allow_inf_nan=False)
    lexical_rank: int | None = Field(default=None, strict=True, gt=0)
    vector_rank: int | None = Field(default=None, strict=True, gt=0)
    lexical_score: float | None = Field(default=None, allow_inf_nan=False)
    vector_score: float | None = Field(default=None, allow_inf_nan=False)


class RetrievalResult(KonwledgeModel):
    """A search evaluated at a single time, including vector coverage gaps."""

    query: str = Field(strict=True, min_length=1)
    mode: RetrievalMode
    evaluated_at: AwareDatetime = Field(strict=True)
    include_stale: bool = Field(strict=True)
    top_k: int = Field(strict=True, gt=0)
    candidate_limit: int = Field(strict=True, gt=0)
    rrf_k: int = Field(strict=True, gt=0)
    hits: tuple[RetrievalHit, ...]
    missing_vector_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> Self:
        if self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        return self

    @property
    def degraded(self) -> bool:
        """True when an eligible unit lacks a usable stored vector."""

        return bool(self.missing_vector_ids)
