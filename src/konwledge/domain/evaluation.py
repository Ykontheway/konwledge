"""Immutable dataset imports and read-only retrieval evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from konwledge.domain.models import KonwledgeModel, _ensure_aware

_NIL_UUID = UUID(int=0)


class EvaluationItem(KonwledgeModel):
    """One stable test case and its expected knowledge lineages."""

    model_config = ConfigDict(strict=True, str_strip_whitespace=False)

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_lineage_ids: frozenset[UUID] = Field(default_factory=frozenset, strict=False)
    expect_no_answer: bool = False
    at: datetime | None = None
    source_id: UUID | None = None
    include_stale: bool = False

    @field_validator("id", "query")
    @classmethod
    def validate_nonblank_text(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value

    @field_validator("expected_lineage_ids", mode="before")
    @classmethod
    def reject_duplicate_lineages(cls, value: Any) -> Any:
        if isinstance(value, Collection) and not isinstance(value, (str, bytes, dict)):
            entries = list(value)
            try:
                canonical = [
                    entry if isinstance(entry, UUID) else UUID(str(entry)) for entry in entries
                ]
            except (TypeError, ValueError, AttributeError):
                return value
            if len(canonical) != len(set(canonical)):
                raise ValueError("expected_lineage_ids must not contain duplicates")
        return value

    @field_validator("expected_lineage_ids")
    @classmethod
    def validate_lineages(cls, value: frozenset[UUID]) -> frozenset[UUID]:
        if _NIL_UUID in value:
            raise ValueError("expected_lineage_ids must not contain the nil UUID")
        return value

    @field_serializer("expected_lineage_ids", when_used="json")
    def serialize_lineages(self, value: frozenset[UUID]) -> list[str]:
        return sorted(str(lineage_id) for lineage_id in value)

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _ensure_aware(value, "at")

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: UUID | None) -> UUID | None:
        if value == _NIL_UUID:
            raise ValueError("source_id must not be the nil UUID")
        return value

    @model_validator(mode="after")
    def validate_answerability(self) -> Self:
        if self.expect_no_answer and self.expected_lineage_ids:
            raise ValueError("no-answer items must have empty expected_lineage_ids")
        if not self.expect_no_answer and not self.expected_lineage_ids:
            raise ValueError("answerable items require expected_lineage_ids")
        return self


class EvaluationSet(KonwledgeModel):
    """One complete immutable version of an evaluation dataset."""

    model_config = ConfigDict(strict=True)

    schema_version: Literal[1] = 1
    id: UUID
    name: str = Field(min_length=1)
    version: int = Field(gt=0)
    created_at: datetime
    description: str | None = None
    items: tuple[EvaluationItem, ...] = Field(min_length=1, strict=False)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: UUID) -> UUID:
        if value == _NIL_UUID:
            raise ValueError("id must not be the nil UUID")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "created_at")

    @field_validator("items")
    @classmethod
    def validate_unique_items(cls, value: tuple[EvaluationItem, ...]) -> tuple[EvaluationItem, ...]:
        item_ids = [item.id for item in value]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("evaluation item IDs must be unique")
        return value

    @property
    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON representation of this full snapshot."""

        return hashlib.sha256(_canonical_json(self).encode("utf-8")).hexdigest()


def _canonical_json(dataset: EvaluationSet) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def dump_evaluation_set(dataset: EvaluationSet) -> str:
    """Export deterministic, human-readable JSON followed by one newline."""

    return (
        json.dumps(
            dataset.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
