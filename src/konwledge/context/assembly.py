"""Pure, deterministic assembly of bounded retrieval context."""

from __future__ import annotations

from uuid import UUID

from konwledge.context.citations import citation_from_hit, validate_hit_evidence
from konwledge.context.models import (
    ContextPacket,
    ContextSkipReason,
    EvidenceBlock,
    SkippedEvidence,
    TokenContextPacket,
    render_context,
)
from konwledge.context.tokens import TokenCounter, count_tokens, resolve_token_counter
from konwledge.domain import KnowledgeUnitStatus
from konwledge.retrieval import RetrievalHit, RetrievalResult


def validate_budget(max_chars: int, max_units: int) -> None:
    """Reject non-integral or out-of-range context limits without coercion."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError("max_chars must be a non-negative integer")
    if isinstance(max_units, bool) or not isinstance(max_units, int) or max_units <= 0:
        raise ValueError("max_units must be a positive integer")


def _is_allowed(hit: RetrievalHit, *, include_stale: bool) -> bool:
    return hit.status is KnowledgeUnitStatus.ACTIVE or (
        include_stale and hit.status is KnowledgeUnitStatus.STALE
    )


def _overlaps(
    hit: RetrievalHit,
    accepted_spans: dict[UUID, list[tuple[int, int]]],
) -> bool:
    spans = accepted_spans.get(hit.document.id, ())
    return any(hit.unit.start_char < end and start < hit.unit.end_char for start, end in spans)


def _block(hit: RetrievalHit, label_number: int) -> EvidenceBlock:
    return EvidenceBlock(
        label=f"[{label_number}]",
        citation=citation_from_hit(hit),
        lineage_id=hit.unit.lineage_id,
        source_uri=hit.source.uri,
        document_checksum=hit.document.checksum,
        section_path=hit.unit.section_path,
        status=hit.status,
        stale_reason=hit.stale_reason,
        score=hit.score,
        lexical_rank=hit.lexical_rank,
        vector_rank=hit.vector_rank,
    )


def assemble_context(
    result: RetrievalResult,
    *,
    max_chars: int = 8000,
    max_units: int = 10,
    max_tokens: int | None = None,
    token_encoding: str = "cl100k_base",
    token_counter: TokenCounter | None = None,
) -> ContextPacket:
    """Select whole exact evidence records in retrieval order within every limit."""

    validate_budget(max_chars, max_units)
    counter = resolve_token_counter(
        max_tokens=max_tokens,
        token_encoding=token_encoding,
        token_counter=token_counter,
    )
    blocks: list[EvidenceBlock] = []
    skipped: list[SkippedEvidence] = []
    accepted_units: set[UUID] = set()
    accepted_lineages: set[UUID] = set()
    accepted_spans: dict[UUID, list[tuple[int, int]]] = {}

    for hit in result.hits:
        validate_hit_evidence(hit)
        unit = hit.unit
        if len(blocks) >= max_units:
            skipped.append(SkippedEvidence(unit_id=unit.id, reason=ContextSkipReason.MAX_UNITS))
            continue
        if not _is_allowed(hit, include_stale=result.include_stale):
            skipped.append(SkippedEvidence(unit_id=unit.id, reason=ContextSkipReason.STATUS))
            continue
        if unit.id in accepted_units or unit.lineage_id in accepted_lineages:
            skipped.append(SkippedEvidence(unit_id=unit.id, reason=ContextSkipReason.DUPLICATE))
            continue
        if _overlaps(hit, accepted_spans):
            skipped.append(SkippedEvidence(unit_id=unit.id, reason=ContextSkipReason.OVERLAP))
            continue

        candidate = _block(hit, len(blocks) + 1)
        candidate_blocks = (*blocks, candidate)
        candidate_context = render_context(candidate_blocks)
        over_char_budget = len(candidate_context) > max_chars
        over_token_budget = (
            counter is not None
            and max_tokens is not None
            and count_tokens(counter, candidate_context) > max_tokens
        )
        if over_char_budget or over_token_budget:
            skipped.append(SkippedEvidence(unit_id=unit.id, reason=ContextSkipReason.BUDGET))
            continue

        blocks.append(candidate)
        accepted_units.add(unit.id)
        accepted_lineages.add(unit.lineage_id)
        accepted_spans.setdefault(hit.document.id, []).append((unit.start_char, unit.end_char))

    context_text = render_context(blocks)
    packet_values = {
        "query": result.query,
        "mode": result.mode,
        "evaluated_at": result.evaluated_at,
        "include_stale": result.include_stale,
        "max_chars": max_chars,
        "max_units": max_units,
        "blocks": tuple(blocks),
        "skipped": tuple(skipped),
        "missing_vector_ids": result.missing_vector_ids,
        "context_text": context_text,
        "used_chars": len(context_text),
    }
    if counter is None or max_tokens is None:
        return ContextPacket.model_validate(packet_values)

    used_tokens = count_tokens(counter, context_text)
    return TokenContextPacket.model_validate(
        {
            **packet_values,
            "max_tokens": max_tokens,
            "used_tokens": used_tokens,
            "token_encoding": token_encoding,
        }
    )
