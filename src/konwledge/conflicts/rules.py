"""Deterministic rules for detecting contradictory knowledge."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from konwledge.domain import ConflictEvidence

RULE_PORT_MISMATCH = "port_mismatch"
PORT_PREDICATE = "port"

# Deliberately limited to named service tokens such as ``order-service``.  A
# generic word such as "service" is not a sufficiently precise subject.
_SERVICE_TOKEN = re.compile(
    r"(?<![a-z0-9-])([a-z0-9]+(?:-[a-z0-9]+)*-service)(?![a-z0-9-])",
    re.IGNORECASE,
)
_PORT_PATTERNS = (
    re.compile(
        r"(?:默认\s*)?监听\s*(?:在\s*)?(?:端口\s*)?[:：=]?\s*"
        r"([0-9]{1,5})(?![0-9])(?:\s*(?:号)?端口)?"
    ),
    re.compile(
        r"\blistens?\s+on\s+(?:the\s+)?port\s*[:=]?\s*([0-9]{1,5})(?![0-9])\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bport\s*=\s*['\"]?([0-9]{1,5})(?![0-9])['\"]?\b", re.IGNORECASE),
)
_NEGATED = re.compile(
    r"(?:不|未|不要|禁止|并非|不是|无需)\s*(?:默认\s*)?监听"
    r"|\b(?:does\s+not|doesn't|do\s+not|don't|not|never)\s+(?:listen|use\s+port)"
    r"|(?:不|未|不要|禁止|并非|不是|无需)\s*(?:使用|设置)\s*\bport\s*=",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"可选|任选|或者|或是|范围|之间|举例|示例|例如|比如"
    r"|(?<=[0-9])\s*(?:/|或)\s*(?=[0-9])"
    r"|\b(?:or|optional|range|one\s+of|for\s+example|e\.g\.)\b",
    re.IGNORECASE,
)
_NUMERIC_RANGE = re.compile(r"[0-9]{1,5}\s*(?:-|~|\uff5e|至|到)\s*[0-9]{1,5}")


@dataclass(frozen=True, slots=True)
class PortAssertion:
    """One unambiguous scalar port statement extracted from a unit."""

    subject: str
    value: str


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    """A stable, storage-independent conflict candidate."""

    key: str
    rule: str
    reason: str
    left: ConflictEvidence
    right: ConflictEvidence


def service_tokens(text: str) -> frozenset[str]:
    """Return normalized named-service tokens present in ``text``."""

    return frozenset(match.group(1).lower() for match in _SERVICE_TOKEN.finditer(text))


def document_service_context(texts: Iterable[str]) -> frozenset[str]:
    """Return every named service mentioned by units in one document head."""

    return frozenset(token for text in texts for token in service_tokens(text))


def extract_port_assertion(
    text: str,
    *,
    document_services: frozenset[str] = frozenset(),
    section_path: tuple[str, ...] = (),
) -> PortAssertion | None:
    """Extract a port only when both its scalar value and service are unambiguous.

    A service named in the unit is authoritative.  A service named in the
    section path is used next.  Document context is used only when no service
    is found and the complete document names exactly one.
    Negations, examples, alternatives, ranges, invalid ports, and units naming
    multiple services are intentionally rejected rather than guessed.
    """

    local_services = service_tokens(text)
    if len(local_services) > 1 or _NEGATED.search(text) or _UNCERTAIN.search(text):
        return None
    if _NUMERIC_RANGE.search(text):
        return None

    values = {int(match.group(1)) for pattern in _PORT_PATTERNS for match in pattern.finditer(text)}
    if len(values) != 1:
        return None
    value = next(iter(values))
    if not 1 <= value <= 65535:
        return None

    if len(local_services) == 1:
        subject = next(iter(local_services))
    else:
        path_services = frozenset(token for part in section_path for token in service_tokens(part))
        if len(path_services) > 1:
            return None
        if len(path_services) == 1:
            subject = next(iter(path_services))
        elif len(document_services) == 1:
            subject = next(iter(document_services))
        else:
            return None
    return PortAssertion(subject=subject, value=str(value))


def port_mismatch_candidates(evidence: Iterable[ConflictEvidence]) -> tuple[ConflictCandidate, ...]:
    """Compare port evidence in subject buckets and return stable candidates."""

    buckets: dict[tuple[str, str], list[ConflictEvidence]] = {}
    for item in evidence:
        if item.predicate != PORT_PREDICATE:
            continue
        buckets.setdefault((item.subject, item.predicate), []).append(item)

    by_key: dict[str, ConflictCandidate] = {}
    for (subject, predicate), items in sorted(buckets.items()):
        ordered = sorted(items, key=_full_evidence_order)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if first.source_id == second.source_id or first.value == second.value:
                    continue
                left, right = sorted((first, second), key=_canonical_evidence_order)
                key = conflict_key(
                    rule=RULE_PORT_MISMATCH,
                    subject=subject,
                    predicate=predicate,
                    left=left,
                    right=right,
                )
                by_key.setdefault(
                    key,
                    ConflictCandidate(
                        key=key,
                        rule=RULE_PORT_MISMATCH,
                        reason=(
                            f"{subject} has conflicting port values: {left.value} and {right.value}"
                        ),
                        left=left,
                        right=right,
                    ),
                )
    return tuple(by_key[key] for key in sorted(by_key))


def conflict_key(
    *,
    rule: str,
    subject: str,
    predicate: str,
    left: ConflictEvidence,
    right: ConflictEvidence,
) -> str:
    """Hash logical evidence identity, excluding version-specific unit/document ids."""

    canonical = sorted((left, right), key=_canonical_evidence_order)
    payload = {
        "rule": rule,
        "subject": subject,
        "predicate": predicate,
        "evidence": [
            {"lineage_id": str(item.lineage_id), "value": item.value} for item in canonical
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _canonical_evidence_order(item: ConflictEvidence) -> tuple[str, str, str]:
    return str(item.source_id), str(item.lineage_id), item.value


def _full_evidence_order(item: ConflictEvidence) -> tuple[str, str, str, str, str]:
    return (*_canonical_evidence_order(item), str(item.document_id), str(item.unit_id))
