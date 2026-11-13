#!/usr/bin/env python3
"""Export the B1 management DTO/query schema bundle for the web client."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from konwledge.api.management_models import (
    DiffItem,
    DiffQuery,
    DocumentDiffDTO,
    DocumentDTO,
    EvidenceDTO,
    JobDTO,
    JobsQuery,
    LineageDTO,
    Page,
    PageQuery,
    RevisionDTO,
    UnitDTO,
)

SCHEMA_ID = "https://github.com/Ykontheway/konwledge/blob/main/web/src/management/schema.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "web" / "src" / "management" / "schema.json"


def _fragment(schema: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    body = {key: value for key, value in schema.items() if key not in {"$defs", "$schema", "$id"}}
    defs = dict(schema.get("$defs", {}))
    return body, defs


def _schema_sources() -> tuple[tuple[str, dict[str, Any]], ...]:
    return (
        ("PageQuery", PageQuery.model_json_schema(mode="validation")),
        ("JobsQuery", JobsQuery.model_json_schema(mode="validation")),
        ("DiffQuery", DiffQuery.model_json_schema(mode="validation")),
        ("JobDTO", JobDTO.model_json_schema(mode="serialization")),
        ("RevisionDTO", RevisionDTO.model_json_schema(mode="serialization")),
        ("UnitDTO", UnitDTO.model_json_schema(mode="serialization")),
        ("DocumentDTO", DocumentDTO.model_json_schema(mode="serialization")),
        ("LineageDTO", LineageDTO.model_json_schema(mode="serialization")),
        ("DocumentDiffDTO", DocumentDiffDTO.model_json_schema(mode="serialization")),
        ("EvidenceDTO", EvidenceDTO.model_json_schema(mode="serialization")),
        ("ListJobsResponse", Page[JobDTO].model_json_schema(mode="serialization")),
        ("DocumentUnitsPage", Page[UnitDTO].model_json_schema(mode="serialization")),
        ("DiffItemPage", Page[DiffItem].model_json_schema(mode="serialization")),
    )


def build_management_schema() -> dict[str, Any]:
    defs: dict[str, Any] = {}
    for name, schema in _schema_sources():
        body, nested_defs = _fragment(schema)
        for nested_name, nested_schema in nested_defs.items():
            if nested_name in defs and defs[nested_name] != nested_schema:
                raise ValueError(f"conflicting schema definition: {nested_name}")
            defs.setdefault(nested_name, nested_schema)
        if name in defs and defs[name] != body:
            raise ValueError(f"conflicting schema fragment: {name}")
        defs[name] = body

    defs["GetJobResponse"] = {"$ref": "#/$defs/JobDTO"}
    defs["GetRevisionResponse"] = {"$ref": "#/$defs/RevisionDTO"}
    defs["GetDocumentResponse"] = {"$ref": "#/$defs/DocumentDTO"}
    defs["GetLineageResponse"] = {"$ref": "#/$defs/LineageDTO"}
    defs["GetDocumentDiffResponse"] = {"$ref": "#/$defs/DocumentDiffDTO"}
    defs["GetUnitResponse"] = {"$ref": "#/$defs/UnitDTO"}
    defs["GetEvidenceResponse"] = {"$ref": "#/$defs/EvidenceDTO"}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "ManagementApiSchemaBundle",
        "description": (
            "Read-only B1 management API queries and DTOs exported from Python models."
        ),
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "$defs": {name: defs[name] for name in sorted(defs)},
    }


def dump_management_schema() -> str:
    return json.dumps(build_management_schema(), ensure_ascii=False, indent=2) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the management API JSON schema bundle for the web client."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Schema output path (default: web/src/management/schema.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed schema does not match the exported schema.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    output = args.output.resolve()
    rendered = dump_management_schema()
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != rendered:
            print(
                f"{output} is out of date; run `python scripts/export_management_schema.py`.",
                file=sys.stderr,
            )
            return 1
        print(f"Management schema is up to date: {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
