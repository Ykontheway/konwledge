"""Context orchestration over the read-only retrieval service."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from konwledge.context.assembly import assemble_context, validate_budget
from konwledge.context.models import ContextPacket
from konwledge.context.tokens import TokenCounter, resolve_token_counter
from konwledge.retrieval import RetrievalMode, RetrievalResult, RetrievalService


class ContextService:
    """Build bounded context from exactly one retrieval snapshot."""

    def __init__(self, retrieval_service: RetrievalService) -> None:
        self._retrieval = retrieval_service

    @property
    def retrieval(self) -> RetrievalService:
        """The retrieval service used by this context service."""

        return self._retrieval

    def assemble(
        self,
        result: RetrievalResult,
        *,
        max_chars: int = 8000,
        max_units: int = 10,
        max_tokens: int | None = None,
        token_encoding: str = "cl100k_base",
        token_counter: TokenCounter | None = None,
    ) -> ContextPacket:
        """Pure wrapper around :func:`assemble_context`."""

        return assemble_context(
            result,
            max_chars=max_chars,
            max_units=max_units,
            max_tokens=max_tokens,
            token_encoding=token_encoding,
            token_counter=token_counter,
        )

    def build(
        self,
        query: str,
        *,
        mode: RetrievalMode = RetrievalMode.LEXICAL,
        top_k: int = 5,
        candidate_limit: int | None = None,
        rrf_k: int = 60,
        at: datetime | None = None,
        include_stale: bool = False,
        source_id: UUID | None = None,
        max_chars: int = 8000,
        max_units: int = 10,
        max_tokens: int | None = None,
        token_encoding: str = "cl100k_base",
        token_counter: TokenCounter | None = None,
    ) -> ContextPacket:
        """Search once, then assemble that result without another clock or query."""

        validate_budget(max_chars, max_units)
        counter = resolve_token_counter(
            max_tokens=max_tokens,
            token_encoding=token_encoding,
            token_counter=token_counter,
        )
        result = self._retrieval.search(
            query,
            mode=mode,
            top_k=top_k,
            candidate_limit=candidate_limit,
            rrf_k=rrf_k,
            at=at,
            include_stale=include_stale,
            source_id=source_id,
        )
        return assemble_context(
            result,
            max_chars=max_chars,
            max_units=max_units,
            max_tokens=max_tokens,
            token_encoding=token_encoding,
            token_counter=counter,
        )
