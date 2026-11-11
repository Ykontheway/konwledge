"""Public package interface for Konwledge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from konwledge.application import KnowledgeLibrary

__all__ = ["KnowledgeLibrary", "__version__"]

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """Load the application facade only when requested from the package root."""

    if name == "KnowledgeLibrary":
        from konwledge.application import KnowledgeLibrary

        globals()[name] = KnowledgeLibrary
        return KnowledgeLibrary
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy public exports in module introspection."""

    return sorted(set(globals()) | set(__all__))
