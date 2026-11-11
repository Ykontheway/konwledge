"""Value types deliberately frozen for the snapshot schema version 1 contract.

These types are independent of the evolving domain model. Adding a domain source
kind must not silently expand the V1 JSON Schema consumed by the frozen V2 site.
"""

from enum import StrEnum


class SourceKind(StrEnum):
    """Supported origin categories for imported knowledge."""

    URL = "url"
    FILE = "file"
    REPOSITORY = "repository"
    MANUAL = "manual"
