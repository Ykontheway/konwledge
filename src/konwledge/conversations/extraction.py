"""Conservative, deterministic extraction of explicit conversation candidates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from konwledge.domain.conversations import (
    CandidateKind,
    Conversation,
    ConversationCandidate,
    ConversationRole,
)

_CANDIDATE_NAMESPACE = uuid5(NAMESPACE_URL, "konwledge:conversation-candidate:v1")
_MARKER = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?(?P<marker>fact|decision|事实|决策)[ \t]*[:：][ \t]*",
    re.IGNORECASE,
)
_ELIGIBLE_ROLES = frozenset({ConversationRole.USER, ConversationRole.ASSISTANT})


def candidate_id(
    conversation: Conversation,
    *,
    message_id: str,
    kind: CandidateKind,
    start_char: int,
    end_char: int,
) -> UUID:
    """Build a stable candidate UUID from immutable source provenance."""

    identity = {
        "conversation_hash": conversation.content_hash,
        "conversation_id": conversation.conversation_id,
        "conversation_version": conversation.version,
        "end_char": end_char,
        "kind": kind.value,
        "message_id": message_id,
        "start_char": start_char,
    }
    return uuid5(
        _CANDIDATE_NAMESPACE,
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def extract_candidates(
    conversation: Conversation, *, created_at: datetime
) -> tuple[ConversationCandidate, ...]:
    """Extract explicit one-line Fact/Decision markers without judging their truth."""

    candidates: list[ConversationCandidate] = []
    for ordinal, message in enumerate(conversation.messages):
        if message.role not in _ELIGIBLE_ROLES:
            continue
        offset = 0
        for line in message.content.splitlines(keepends=True):
            body = line
            if body.endswith("\n"):
                body = body[:-1]
                if body.endswith("\r"):
                    body = body[:-1]
            match = _MARKER.match(body)
            if match is not None:
                start_in_line = match.end()
                end_in_line = len(body)
                while start_in_line < end_in_line and body[start_in_line] in " \t":
                    start_in_line += 1
                while end_in_line > start_in_line and body[end_in_line - 1] in " \t":
                    end_in_line -= 1
                text = body[start_in_line:end_in_line]
                if text and not text.endswith(("?", "？")):
                    marker = match.group("marker").casefold()
                    kind = (
                        CandidateKind.FACT if marker in {"fact", "事实"} else CandidateKind.DECISION
                    )
                    start = offset + start_in_line
                    end = offset + end_in_line
                    candidates.append(
                        ConversationCandidate(
                            id=candidate_id(
                                conversation,
                                message_id=message.id,
                                kind=kind,
                                start_char=start,
                                end_char=end,
                            ),
                            conversation_id=conversation.conversation_id,
                            conversation_version=conversation.version,
                            conversation_hash=conversation.content_hash,
                            message_id=message.id,
                            message_ordinal=ordinal,
                            role=message.role,
                            kind=kind,
                            start_char=start,
                            end_char=end,
                            text=text,
                            created_at=created_at,
                        )
                    )
            offset += len(line)
        # ``splitlines(keepends=True)`` returns no item only for the empty string.
    return tuple(candidates)


def verify_candidate(conversation: Conversation, candidate: ConversationCandidate) -> None:
    """Fail closed if stored conversation/candidate provenance is inconsistent or truncated."""

    if (
        candidate.conversation_id != conversation.conversation_id
        or candidate.conversation_version != conversation.version
        or candidate.conversation_hash != conversation.content_hash
    ):
        raise ValueError("candidate conversation identity/hash does not match stored conversation")
    if candidate.message_ordinal >= len(conversation.messages):
        raise ValueError("candidate message ordinal is outside the stored conversation")
    message = conversation.messages[candidate.message_ordinal]
    if message.id != candidate.message_id or message.role is not candidate.role:
        raise ValueError("candidate message identity/role does not match stored conversation")
    if candidate.end_char > len(message.content):
        raise ValueError("candidate span is truncated or outside the original message")
    if message.content[candidate.start_char : candidate.end_char] != candidate.text:
        raise ValueError("candidate text is not the exact stored message slice")
    expected_id = candidate_id(
        conversation,
        message_id=message.id,
        kind=candidate.kind,
        start_char=candidate.start_char,
        end_char=candidate.end_char,
    )
    if candidate.id != expected_id:
        raise ValueError("candidate id does not match its deterministic provenance")
    expected_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
    if not expected_hash:  # pragma: no cover - SHA-256 is always non-empty
        raise ValueError("candidate text could not be fingerprinted")
