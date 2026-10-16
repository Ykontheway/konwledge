"""Load UTF-8 Markdown and plain-text files into normalized memory values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from konwledge.domain import DocumentContentType
from konwledge.ingestion.normalization import fingerprint_text, normalize_text


class UnsupportedFileTypeError(ValueError):
    """Raised when a loader does not support a file extension."""


@dataclass(frozen=True, slots=True)
class LoadedText:
    """Normalized file content and metadata needed to create a document."""

    path: Path
    content: str
    content_type: DocumentContentType
    checksum: str


class TextFileLoader:
    """Load UTF-8 Markdown and TXT files without splitting their content."""

    _CONTENT_TYPES: ClassVar[dict[str, DocumentContentType]] = {
        ".md": DocumentContentType.MARKDOWN,
        ".markdown": DocumentContentType.MARKDOWN,
        ".txt": DocumentContentType.TEXT,
    }

    def load(self, path: str | Path) -> LoadedText:
        file_path = Path(path)
        content_type = self.content_type_for(file_path)
        raw_content = file_path.read_text(encoding="utf-8-sig")
        content = normalize_text(raw_content)
        return LoadedText(
            path=file_path,
            content=content,
            content_type=content_type,
            checksum=fingerprint_text(content),
        )

    def content_type_for(self, path: str | Path) -> DocumentContentType:
        suffix = Path(path).suffix.lower()
        try:
            return self._CONTENT_TYPES[suffix]
        except KeyError as error:
            supported = ", ".join(sorted(self._CONTENT_TYPES))
            raise UnsupportedFileTypeError(
                f"unsupported file extension {suffix or '<none>'}; expected one of: {supported}"
            ) from error
