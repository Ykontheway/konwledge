"""Deterministic citations backed by exact retrieval evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from konwledge.domain import Citation
from konwledge.retrieval.models import RetrievalHit

_CITATION_NAMESPACE = uuid5(NAMESPACE_URL, "konwledge:context-citation:v1")
_METADATA_ANCHORS = (
    "source_uri",
    "lineage_id",
    "section_path",
    "document_checksum",
)


class CitationEvidenceError(ValueError):
    """Raised when retrieval evidence cannot support an exact citation."""


def validate_hit_evidence(hit: RetrievalHit) -> None:
    """Validate the source-document-unit graph and the unit's global offsets."""

    source = hit.source
    document = hit.document
    unit = hit.unit

    if document.source_id != source.id:
        raise CitationEvidenceError("retrieval hit document.source_id does not match source.id")
    if unit.document_id != document.id:
        raise CitationEvidenceError("retrieval hit unit.document_id does not match document.id")
    if unit.start_char < 0:
        raise CitationEvidenceError("retrieval hit unit.start_char must be non-negative")
    if unit.end_char <= unit.start_char:
        raise CitationEvidenceError(
            "retrieval hit unit.end_char must be greater than unit.start_char"
        )
    if unit.end_char - unit.start_char != len(unit.text):
        raise CitationEvidenceError(
            "retrieval hit unit global offset range must equal len(unit.text)"
        )
    if not unit.text.strip():
        raise CitationEvidenceError("retrieval hit unit.text must contain non-whitespace evidence")


def _citation_metadata(hit: RetrievalHit) -> dict[str, Any]:
    return {
        "source_uri": hit.source.uri,
        "lineage_id": str(hit.unit.lineage_id),
        "section_path": list(hit.unit.section_path),
        "document_checksum": hit.document.checksum,
    }


def _citation_id(hit: RetrievalHit) -> UUID:
    unit = hit.unit
    identity = {
        "document_id": str(hit.document.id),
        "end": unit.end_char,
        "sha256quote": hashlib.sha256(unit.text.encode("utf-8")).hexdigest(),
        "source_id": str(hit.source.id),
        "start": unit.start_char,
        "unit_id": str(unit.id),
    }
    canonical_json = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_CITATION_NAMESPACE, canonical_json)


def citation_from_hit(hit: RetrievalHit) -> Citation:
    """Create a stable citation from validated, complete retrieval evidence."""

    validate_hit_evidence(hit)
    return Citation(
        id=_citation_id(hit),
        source_id=hit.source.id,
        document_id=hit.document.id,
        knowledge_unit_id=hit.unit.id,
        quote=hit.unit.text,
        locator=hit.document.locator,
        start_char=hit.unit.start_char,
        end_char=hit.unit.end_char,
        confidence=None,
        metadata=_citation_metadata(hit),
    )


def verify_citation(citation: Citation, hit: RetrievalHit) -> None:
    """Raise when a citation does not exactly correspond to a retrieval hit."""

    validate_hit_evidence(hit)
    expected_fields = {
        "source_id": hit.source.id,
        "document_id": hit.document.id,
        "knowledge_unit_id": hit.unit.id,
        "quote": hit.unit.text,
        "locator": hit.document.locator,
    }
    if citation.start_char is not None and citation.end_char is not None:
        expected_fields.update(
            start_char=hit.unit.start_char,
            end_char=hit.unit.end_char,
        )
    for field, expected in expected_fields.items():
        actual = getattr(citation, field)
        if actual != expected:
            raise CitationEvidenceError(f"citation.{field} does not match retrieval hit evidence")

    expected_metadata = _citation_metadata(hit)
    for key in _METADATA_ANCHORS:
        if key in citation.metadata and citation.metadata[key] != expected_metadata[key]:
            raise CitationEvidenceError(
                f"citation metadata {key!r} does not match retrieval hit evidence"
            )
