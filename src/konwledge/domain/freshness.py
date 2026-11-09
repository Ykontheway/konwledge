"""Source-scoped freshness rules; configurations are stored in revision payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from konwledge.domain.models import KonwledgeModel, _ensure_aware


class FreshnessPolicy(KonwledgeModel):
    """Configuration for when knowledge units should be considered stale."""

    ttl_seconds: int | None = Field(default=None)
    source_updated_at: datetime | None = Field(default=None)

    @field_validator("ttl_seconds", mode="before")
    @classmethod
    def validate_ttl_seconds(cls, value: Any) -> Any:
        if value is None:
            return None
        # Must be strictly positive int. Reject bool (which is an int subclass) and float.
        if isinstance(value, bool):
            raise ValueError("ttl_seconds must be an integer, not a boolean")
        if not isinstance(value, int):
            raise ValueError("ttl_seconds must be an integer")
        if value <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        # The largest whole-second interval representable between datetime.min/max.
        if value > 315_537_897_599:
            raise ValueError("ttl_seconds exceeds datetime's supported range")
        return value

    @field_validator("source_updated_at")
    @classmethod
    def validate_source_updated_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _ensure_aware(value, "source_updated_at")
