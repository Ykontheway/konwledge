"""Optional, exact token counting for context budgets."""

from __future__ import annotations

import importlib
from typing import Any, Protocol


class TokenCounter(Protocol):
    """Structural interface for deterministic context token counters."""

    def count(self, text: str, /) -> int:
        """Return the exact token count for ``text``."""


class TiktokenTokenCounter:
    """Exact token counter backed by a lazily imported tiktoken encoding."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        _validate_encoding_name(encoding_name)
        try:
            tiktoken = importlib.import_module("tiktoken")
        except (ImportError, ModuleNotFoundError) as exc:
            raise ImportError(
                "Token budgeting requires the optional 'tiktoken' dependency; "
                "install konwledge[tokens]"
            ) from exc

        try:
            self._encoding: Any = tiktoken.get_encoding(encoding_name)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown tiktoken encoding: {encoding_name!r}") from exc
        self.encoding_name = encoding_name

    def count(self, text: str, /) -> int:
        """Count text exactly, treating special-token spellings as ordinary evidence."""

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return len(self._encoding.encode(text, disallowed_special=()))


def _validate_encoding_name(token_encoding: object) -> None:
    if not isinstance(token_encoding, str) or not token_encoding.strip():
        raise ValueError("token_encoding must be a non-empty string")


def resolve_token_counter(
    *,
    max_tokens: int | None,
    token_encoding: str,
    token_counter: TokenCounter | None,
) -> TokenCounter | None:
    """Validate token-budget inputs and preload the selected exact counter."""

    if max_tokens is None:
        return None
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 0:
        raise ValueError("max_tokens must be a non-negative integer or None")
    _validate_encoding_name(token_encoding)

    if token_counter is None:
        return TiktokenTokenCounter(token_encoding)
    if not callable(getattr(token_counter, "count", None)):
        raise ValueError("token_counter must provide a callable count(text) method")
    return token_counter


def count_tokens(counter: TokenCounter, text: str) -> int:
    """Count and strictly validate a counter result without coercion."""

    result = counter.count(text)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("token_counter.count(text) must return a non-negative integer")
    return result
