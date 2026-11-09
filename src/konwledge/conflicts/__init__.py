"""Deterministic conflict detection and human-review API."""

from konwledge.conflicts.rules import ConflictCandidate
from konwledge.conflicts.service import (
    ConflictNotFoundError,
    ConflictScan,
    ConflictService,
    ConflictTransitionError,
)

__all__ = [
    "ConflictCandidate",
    "ConflictNotFoundError",
    "ConflictScan",
    "ConflictService",
    "ConflictTransitionError",
]
