"""Knowledge lifecycle: version diffs, lineage, and revision chains."""

from konwledge.lifecycle.diff import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DocumentDiff,
    UnitChange,
    UnitChangeKind,
    diff_units,
)
from konwledge.lifecycle.revisions import initial_revisions, revisions_for_diff

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DocumentDiff",
    "UnitChange",
    "UnitChangeKind",
    "diff_units",
    "initial_revisions",
    "revisions_for_diff",
]
