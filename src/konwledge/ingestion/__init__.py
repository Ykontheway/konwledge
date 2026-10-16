"""Text ingestion primitives and application services."""

from konwledge.ingestion.loaders import LoadedText, TextFileLoader, UnsupportedFileTypeError
from konwledge.ingestion.normalization import fingerprint_text, normalize_text
from konwledge.ingestion.service import (
    FileIngestionService,
    IngestionResult,
    SourceNotFoundError,
    UnsupportedSourceKindError,
)

__all__ = [
    "FileIngestionService",
    "IngestionResult",
    "LoadedText",
    "SourceNotFoundError",
    "TextFileLoader",
    "UnsupportedFileTypeError",
    "UnsupportedSourceKindError",
    "fingerprint_text",
    "normalize_text",
]
