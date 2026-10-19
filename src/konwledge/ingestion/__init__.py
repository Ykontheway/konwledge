"""Text ingestion primitives and application services."""

from konwledge.ingestion.loaders import LoadedText, TextFileLoader, UnsupportedFileTypeError
from konwledge.ingestion.normalization import fingerprint_text, normalize_text
from konwledge.ingestion.pipeline import (
    KnowledgeUnitService,
    SplittingResult,
    TraceableIngestionResult,
    TraceableIngestionService,
)
from konwledge.ingestion.service import (
    FileIngestionService,
    IngestionResult,
    SourceNotFoundError,
    UnsupportedSourceKindError,
)
from konwledge.ingestion.splitting import TraceableTextSplitter

__all__ = [
    "FileIngestionService",
    "IngestionResult",
    "KnowledgeUnitService",
    "LoadedText",
    "SourceNotFoundError",
    "SplittingResult",
    "TextFileLoader",
    "TraceableIngestionResult",
    "TraceableIngestionService",
    "TraceableTextSplitter",
    "UnsupportedFileTypeError",
    "UnsupportedSourceKindError",
    "fingerprint_text",
    "normalize_text",
]
