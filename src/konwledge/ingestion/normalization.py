"""Deterministic text normalization and content fingerprinting."""

from __future__ import annotations

from hashlib import sha256
from unicodedata import normalize


def normalize_text(text: str) -> str:
    """Normalize equivalent Unicode and line endings without changing layout."""

    normalized_newlines = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalize("NFC", normalized_newlines)


def fingerprint_text(text: str) -> str:
    """Return the lowercase SHA-256 fingerprint of normalized UTF-8 text."""

    return sha256(text.encode("utf-8")).hexdigest()
