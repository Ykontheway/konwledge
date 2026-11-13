from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator

from scripts.export_management_schema import (
    DEFAULT_OUTPUT,
    build_management_schema,
    dump_management_schema,
)

OUTPUT_DEF_NAMES = (
    "JobDTO",
    "RevisionDTO",
    "UnitDTO",
    "LineageDTO",
    "DocumentDTO",
    "DocumentDiffDTO",
    "EvidenceDTO",
    "ListJobsResponse",
    "GetJobResponse",
    "GetRevisionResponse",
    "GetDocumentResponse",
    "GetLineageResponse",
    "GetDocumentDiffResponse",
    "GetUnitResponse",
    "GetEvidenceResponse",
)
FORBIDDEN_OUTPUT_FIELDS = {"query", "locator", "metadata"}


def _iter_refs(
    schema: dict[str, object], definition: dict[str, object]
) -> Iterator[dict[str, object]]:
    stack: list[dict[str, object]] = [definition]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        reference = current.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name not in seen:
                seen.add(name)
                definitions = schema["$defs"]
                assert isinstance(definitions, dict)
                target = definitions[name]
                assert isinstance(target, dict)
                stack.append(target)
            continue
        yield current
        for value in current.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        stack.append(item)


def test_management_schema_matches_committed_file() -> None:
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == dump_management_schema()


def test_management_schema_check_cli_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/export_management_schema.py", "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_management_schema_omits_query_locator_and_metadata_from_outputs() -> None:
    schema = build_management_schema()
    for name in OUTPUT_DEF_NAMES:
        definition = schema["$defs"][name]
        assert isinstance(definition, dict)
        for node in _iter_refs(schema, definition):
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert FORBIDDEN_OUTPUT_FIELDS.isdisjoint(properties)


def test_management_schema_bundle_contains_expected_roots() -> None:
    committed = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    for name in (
        "PageQuery",
        "JobsQuery",
        "DiffQuery",
        "JobDTO",
        "RevisionDTO",
        "DocumentDTO",
        "LineageDTO",
        "DocumentDiffDTO",
        "UnitDTO",
        "EvidenceDTO",
        "ListJobsResponse",
    ):
        assert name in committed["$defs"]
