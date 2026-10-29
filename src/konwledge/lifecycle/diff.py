"""Unit-level diff between two versions of the same document.

The diff answers one question for incremental updates: which knowledge units
actually changed? It matches the previous version's units against the next
version's units in three passes:

1. **Exact, in order** - ``difflib.SequenceMatcher`` aligns units by content
   hash, so inserting a section does not disturb the matches around it.
2. **Exact, out of order** - leftover units with identical text are paired
   too, so reordering a document never forces re-embedding.
3. **Similar** - the remaining units are paired greedily by text similarity,
   with a bonus for staying in the same section. Pairs above the threshold (or
   in the same named section) are modifications; everything else is an
   addition or a removal.

Exact matches are ``unchanged`` when they stay in the same section and
``moved`` when their section path changed; only additions and modifications
need new embeddings.

The result is deterministic for the same inputs, which keeps revision chains
and index updates reproducible.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from konwledge.domain import KnowledgeUnit

DEFAULT_SIMILARITY_THRESHOLD = 0.5
_SAME_SECTION_BONUS = 0.5


class UnitChangeKind(StrEnum):
    """How one knowledge unit changed between two document versions."""

    ADDED = "added"
    MODIFIED = "modified"
    MOVED = "moved"
    UNCHANGED = "unchanged"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class UnitChange:
    """One matched (or unmatched) pair of units across two versions."""

    kind: UnitChangeKind
    old: KnowledgeUnit | None
    new: KnowledgeUnit | None
    similarity: float

    @property
    def needs_embedding(self) -> bool:
        """Whether the new unit's text differs from anything already embedded."""

        return self.kind in {UnitChangeKind.ADDED, UnitChangeKind.MODIFIED}


@dataclass(frozen=True, slots=True)
class DocumentDiff:
    """Ordered unit changes: new-version order first, then removals."""

    changes: tuple[UnitChange, ...]

    def of_kind(self, kind: UnitChangeKind) -> tuple[UnitChange, ...]:
        return tuple(change for change in self.changes if change.kind is kind)

    @property
    def is_noop(self) -> bool:
        return all(change.kind is UnitChangeKind.UNCHANGED for change in self.changes)

    @property
    def affected(self) -> tuple[UnitChange, ...]:
        """Changes whose new unit must be re-embedded."""

        return tuple(change for change in self.changes if change.needs_embedding)

    @property
    def new_units(self) -> tuple[KnowledgeUnit, ...]:
        return tuple(change.new for change in self.changes if change.new is not None)

    def counts(self) -> dict[str, int]:
        """Number of changes per kind, including zero counts, in a stable order."""

        totals = dict.fromkeys((kind.value for kind in UnitChangeKind), 0)
        for change in self.changes:
            totals[change.kind.value] += 1
        return totals

    def summary(self) -> str:
        """Compact human-readable counts such as ``+1 ~2 >0 =5 -1``."""

        counts = self.counts()
        return (
            f"+{counts['added']} ~{counts['modified']} >{counts['moved']} "
            f"={counts['unchanged']} -{counts['removed']}"
        )

    def with_inherited_lineage(self) -> DocumentDiff:
        """Return a diff whose new units continue the lineage of their matched old unit.

        Added units keep their own lineage. Use this when the new units were
        freshly split and have not been persisted yet.
        """

        changes: list[UnitChange] = []
        for change in self.changes:
            if change.old is not None and change.new is not None:
                inherited = change.new.model_copy(update={"lineage_id": change.old.lineage_id})
                changes.append(
                    UnitChange(
                        kind=change.kind,
                        old=change.old,
                        new=inherited,
                        similarity=change.similarity,
                    )
                )
            else:
                changes.append(change)
        return DocumentDiff(changes=tuple(changes))


def diff_units(
    old_units: Sequence[KnowledgeUnit],
    new_units: Sequence[KnowledgeUnit],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> DocumentDiff:
    """Match ``old_units`` to ``new_units`` and classify every change."""

    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0 and 1")

    old = sorted(old_units, key=lambda unit: unit.ordinal)
    new = sorted(new_units, key=lambda unit: unit.ordinal)
    matched_new: dict[int, UnitChange] = {}
    free_old: list[int] = []
    free_new: list[int] = []

    matcher = SequenceMatcher(
        a=[unit.content_hash for unit in old],
        b=[unit.content_hash for unit in new],
        autojunk=False,
    )
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(old_end - old_start):
                old_unit = old[old_start + offset]
                new_unit = new[new_start + offset]
                matched_new[new_start + offset] = UnitChange(
                    _exact_kind(old_unit, new_unit), old_unit, new_unit, 1.0
                )
        else:
            free_old.extend(range(old_start, old_end))
            free_new.extend(range(new_start, new_end))

    free_old, free_new = _pair_moves(old, new, free_old, free_new, matched_new)
    removed = _pair_similar(old, new, free_old, free_new, matched_new, similarity_threshold)

    changes = [
        matched_new.get(index) or UnitChange(UnitChangeKind.ADDED, None, new[index], 0.0)
        for index in range(len(new))
    ]
    changes.extend(
        UnitChange(UnitChangeKind.REMOVED, old[index], None, 0.0) for index in sorted(removed)
    )
    return DocumentDiff(changes=tuple(changes))


def _exact_kind(old_unit: KnowledgeUnit, new_unit: KnowledgeUnit) -> UnitChangeKind:
    """Identical text is ``unchanged`` in the same section and ``moved`` otherwise.

    A shifted ordinal alone does not count as a move: the knowledge and its
    context are the same, only neighbouring units were inserted or removed.
    """

    if old_unit.section_path == new_unit.section_path:
        return UnitChangeKind.UNCHANGED
    return UnitChangeKind.MOVED


def _pair_moves(
    old: Sequence[KnowledgeUnit],
    new: Sequence[KnowledgeUnit],
    free_old: list[int],
    free_new: list[int],
    matched_new: dict[int, UnitChange],
) -> tuple[list[int], list[int]]:
    by_hash: dict[str, list[int]] = defaultdict(list)
    for index in free_old:
        by_hash[old[index].content_hash].append(index)

    remaining_new: list[int] = []
    used_old: set[int] = set()
    for new_index in free_new:
        candidates = by_hash.get(new[new_index].content_hash)
        if candidates:
            old_index = candidates.pop(0)
            used_old.add(old_index)
            matched_new[new_index] = UnitChange(
                _exact_kind(old[old_index], new[new_index]), old[old_index], new[new_index], 1.0
            )
        else:
            remaining_new.append(new_index)
    return [index for index in free_old if index not in used_old], remaining_new


def _pair_similar(
    old: Sequence[KnowledgeUnit],
    new: Sequence[KnowledgeUnit],
    free_old: list[int],
    free_new: list[int],
    matched_new: dict[int, UnitChange],
    threshold: float,
) -> set[int]:
    candidates: list[tuple[float, int, int, float]] = []
    for old_index in free_old:
        for new_index in free_new:
            old_unit, new_unit = old[old_index], new[new_index]
            same_section = bool(old_unit.section_path) and (
                old_unit.section_path == new_unit.section_path
            )
            ratio = SequenceMatcher(a=old_unit.text, b=new_unit.text, autojunk=False).ratio()
            if ratio < threshold and not same_section:
                continue
            score = ratio + (_SAME_SECTION_BONUS if same_section else 0.0)
            candidates.append((score, old_index, new_index, ratio))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _score, old_index, new_index, ratio in candidates:
        if old_index in used_old or new_index in used_new:
            continue
        used_old.add(old_index)
        used_new.add(new_index)
        matched_new[new_index] = UnitChange(
            UnitChangeKind.MODIFIED, old[old_index], new[new_index], round(ratio, 4)
        )
    return {index for index in free_old if index not in used_old}
