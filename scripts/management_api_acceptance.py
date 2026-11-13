"""B1 管理 API 真实验收库 HTTP 端到端验收。

本脚本只面向 scripts/real_knowledge_acceptance.py 生成的真实验收结果库，用于
重复演示 B1 管理 HTTP 链路；不是通用生产巡检或迁移工具。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypedDict, cast
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx  # noqa: E402

from konwledge.api.app import create_app  # noqa: E402
from konwledge.application import KnowledgeLibrary  # noqa: E402
from konwledge.storage import create_session_factory, create_sqlite_engine  # noqa: E402

PREFIX = "/v1/management"
DEFAULT_DATABASE = Path(
    ".konwledge/real-knowledge-acceptance/20261113T062525019181Z-11d25751/knowledge.db"
)
DEFAULT_REPORT = Path(".konwledge/b1-management-acceptance.json")
WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}
FORBIDDEN_TEXT_KEYS = {"text", "query"}
FORBIDDEN_SAFE_KEYS = {"metadata", "locator", "rawlocator", "rawpayload"}
FORBIDDEN_SUBSTRINGS = ("sqlite+pysqlite:", "DSN", "rawlocator", "rawpayload")


class AcceptanceError(RuntimeError):
    """只携带可公开错误码，避免把路径、query 或异常明文写入报告。"""


class Snapshot(TypedDict):
    digest: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class RouteResult:
    status_code: int
    body: Any
    text: str


@dataclass(frozen=True)
class Navigation:
    job_id: str
    revision_id: str
    old_document_id: str
    new_document_id: str
    old_unit_id: str
    new_unit_id: str
    lineage_id: str


class Runner:
    def __init__(self, database: Path, report: Path) -> None:
        self.database = database.expanduser()
        self.report = report.expanduser()
        self.checks: dict[str, bool] = {}
        self.routes: dict[str, int] = {}
        self.statuses: list[str] = []
        self.ids: dict[str, str] = {}
        self.metrics: dict[str, float | int | bool | None] = {}
        self.counts: dict[str, Any] = {}
        self._key = secrets.token_urlsafe(32)

    def fail(self, code: str) -> NoReturn:
        raise AcceptanceError(code)

    def check(self, name: str, condition: bool, code: str | None = None) -> None:
        self.checks[name] = condition
        if not condition:
            self.fail(code or name.upper())

    def safe_report_value(self, value: object) -> None:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        forbidden_paths = {
            str(ROOT),
            str(ROOT.resolve()),
            str(self.database),
            str(self.database.resolve()),
        }
        self.check(
            "report_no_absolute_path",
            not any(path and path in rendered for path in forbidden_paths),
        )
        self.check("report_no_runtime_token", self._key not in rendered)
        self.check("report_no_database_url", "sqlite+pysqlite:" not in rendered)
        lowered = rendered.lower()
        for forbidden in ("api_key", "apikey", "source", "query", "rawlocator", "rawpayload"):
            if forbidden in lowered:
                self.fail("REPORT_PRIVATE_FIELD_LEAK")

    def validate_inputs(self) -> None:
        self.check("database_arg_required", self.database != Path())
        self.check("database_exists", self.database.is_file(), "DATABASE_NOT_FOUND")
        self.check("report_parent_exists_or_creatable", self.report.parent != Path(""))
        if self.report.exists():
            self.fail("REPORT_EXISTS")

    def sqlite_connect(self) -> sqlite3.Connection:
        uri = self.database.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def uuid_text(value: object) -> str:
        try:
            return str(UUID(str(value)))
        except ValueError as error:
            raise AcceptanceError("INVALID_UUID_IN_DATABASE") from error

    @staticmethod
    def cell_bytes(value: object) -> bytes:
        if value is None:
            return b"N;"
        if isinstance(value, bytes):
            return b"B" + hashlib.sha256(value).hexdigest().encode() + b";"
        return ("T" + repr(value) + ";").encode("utf-8", "surrogatepass")

    def snapshot(self) -> Snapshot:
        digest = hashlib.sha256()
        counts: dict[str, int] = {}
        with self.sqlite_connect() as connection:
            tables = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            self.check(
                "required_tables_present",
                "auto_evaluation_jobs" in tables and "revisions" in tables,
            )
            for table in tables:
                columns = [
                    str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                digest.update(table.encode())
                digest.update(json.dumps(columns, sort_keys=True).encode())
                count = 0
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    count += 1
                    for column in columns:
                        digest.update(column.encode())
                        digest.update(self.cell_bytes(row[column]))
                counts[table] = count
        return {"digest": digest.hexdigest(), "row_counts": counts}

    @staticmethod
    def public_counts(row_counts: Mapping[str, int]) -> dict[str, int]:
        fts_rows = sum(count for table, count in row_counts.items() if "fts" in table)
        selected = {
            "job_rows": row_counts.get("auto_evaluation_jobs", 0),
            "document_rows": row_counts.get("documents", 0),
            "unit_rows": row_counts.get("knowledge_units", 0),
            "revision_rows": row_counts.get("revisions", 0),
            "evaluation_rows": row_counts.get("evaluation_sets", 0),
            "fts_rows": fts_rows,
        }
        selected["table_count"] = len(row_counts)
        selected["total_rows"] = sum(row_counts.values())
        return selected

    def open_library(self) -> tuple[Any, KnowledgeLibrary]:
        engine = create_sqlite_engine(f"sqlite+pysqlite:///{self.database.as_posix()}")
        library = KnowledgeLibrary(create_session_factory(engine))
        return engine, library

    async def call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        auth: str | None = "correct",
    ) -> RouteResult:
        headers: dict[str, str] = {}
        if auth == "correct":
            headers["Authorization"] = f"Bearer {self._key}"
        elif auth == "wrong":
            headers["Authorization"] = "Bearer " + "wrong"
        response = await client.request(method, path, headers=headers)
        self.routes[f"{method} {path.split('?')[0]}"] = response.status_code
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return RouteResult(response.status_code, body, response.text)

    @staticmethod
    def contains_key(value: object, keys: set[str]) -> bool:
        if isinstance(value, Mapping):
            return any(
                str(key).lower() in keys or Runner.contains_key(item, keys)
                for key, item in value.items()
            )
        if isinstance(value, list | tuple):
            return any(Runner.contains_key(item, keys) for item in value)
        return False

    def assert_safe_response(self, result: RouteResult, *, evidence: bool = False) -> None:
        self.check("response_no_runtime_token", self._key not in result.text)
        absolute = (str(ROOT.resolve()), str(self.database.resolve()))
        self.check("response_no_absolute_path", not any(path in result.text for path in absolute))
        self.check(
            "response_no_forbidden_substrings",
            not any(token in result.text for token in FORBIDDEN_SUBSTRINGS),
        )
        self.check(
            "response_no_private_keys", not self.contains_key(result.body, FORBIDDEN_SAFE_KEYS)
        )
        if not evidence:
            self.check(
                "non_evidence_no_private_body_fields",
                not self.contains_key(result.body, FORBIDDEN_TEXT_KEYS),
            )

    async def verify_enablement(self, library: KnowledgeLibrary) -> httpx.AsyncClient:
        disabled = create_app(library, api_key=self._key)
        disabled_transport = httpx.ASGITransport(app=disabled)
        async with httpx.AsyncClient(
            transport=disabled_transport, base_url="http://asgi"
        ) as client:
            missing = await self.call(client, "GET", f"{PREFIX}/jobs")
            self.check("management_default_404", missing.status_code == 404)
            self.check(
                "management_hidden_from_openapi",
                f"{PREFIX}/jobs" not in disabled.openapi()["paths"],
            )

        enabled = create_app(library, api_key=self._key, enable_management=True)
        self.check("management_present_in_openapi", f"{PREFIX}/jobs" in enabled.openapi()["paths"])
        transport = httpx.ASGITransport(app=enabled)
        client = httpx.AsyncClient(transport=transport, base_url="http://asgi")
        no_auth = await self.call(client, "GET", f"{PREFIX}/jobs", auth=None)
        wrong = await self.call(client, "GET", f"{PREFIX}/jobs", auth="wrong")
        correct = await self.call(client, "GET", f"{PREFIX}/jobs")
        self.check("management_no_auth_401", no_auth.status_code == 401)
        self.check("management_wrong_auth_401", wrong.status_code == 401)
        self.check("management_correct_auth_200", correct.status_code == 200)
        self.assert_safe_response(no_auth)
        self.assert_safe_response(wrong)
        self.assert_safe_response(correct)
        return client

    async def require_get(
        self, client: httpx.AsyncClient, path: str, *, evidence: bool = False
    ) -> Any:
        result = await self.call(client, "GET", path)
        self.check("route_status_200", result.status_code == 200, "HTTP_ROUTE_NOT_200")
        self.assert_safe_response(result, evidence=evidence)
        return result.body

    def validate_jobs(
        self, jobs: Mapping[str, Any]
    ) -> tuple[list[Mapping[str, Any]], dict[str, str]]:
        items = cast(list[Mapping[str, Any]], jobs.get("items"))
        self.check("job_total_at_least_three", isinstance(items, list) and len(items) >= 3)
        first_three = items[:3]
        statuses = [str(job.get("status")) for job in first_three]
        self.statuses = statuses
        self.check(
            "job_statuses_desc_passed_failed_passed", statuses == ["passed", "failed", "passed"]
        )
        mapping = {
            "restore": str(first_three[0]["id"]),
            "fault": str(first_three[1]["id"]),
            "positive": str(first_three[2]["id"]),
        }
        self.ids.update({f"job_{name}": identifier for name, identifier in mapping.items()})
        self.counts["jobs_total"] = int(jobs["total"])
        self.counts["jobs_page_count"] = len(items)
        return first_three, mapping

    async def validate_job_detail(
        self, client: httpx.AsyncClient, job_id: str
    ) -> Mapping[str, Any]:
        job = cast(Mapping[str, Any], await self.require_get(client, f"{PREFIX}/jobs/{job_id}"))
        report = cast(Mapping[str, Any], job.get("report"))
        metrics = cast(Mapping[str, Any], report.get("metrics"))
        checks = cast(list[Mapping[str, Any]], report.get("checks"))
        revisions = cast(list[str], job.get("trigger_revision_ids"))
        self.check("job_detail_status_failed", job.get("status") == "failed")
        self.check("job_detail_report_readable", isinstance(report, Mapping) and bool(report))
        self.check("job_detail_mean_recall_present", metrics.get("mean_recall") is not None)
        self.check("job_detail_mrr_present", metrics.get("mean_reciprocal_rank") is not None)
        self.check("job_detail_checks_present", len(checks) > 0)
        self.check("job_detail_trigger_revisions", len(revisions) > 0)
        self.metrics.update(
            {
                "case_count": cast(int, metrics.get("case_count")),
                "mean_recall": cast(float | int | None, metrics.get("mean_recall")),
                "mean_reciprocal_rank": cast(
                    float | int | None, metrics.get("mean_reciprocal_rank")
                ),
                "report_passed": cast(bool, report.get("passed")),
                "safe_check_count": len(checks),
            }
        )
        self.counts["fault_trigger_revision_count"] = len(revisions)
        return job

    async def find_navigation(
        self, client: httpx.AsyncClient, revision_ids: Sequence[str]
    ) -> tuple[Navigation, Mapping[str, Any]]:
        fallback: tuple[Navigation, Mapping[str, Any]] | None = None
        for revision_id in revision_ids:
            revision = cast(
                Mapping[str, Any],
                await self.require_get(client, f"{PREFIX}/revisions/{revision_id}"),
            )
            nav = cast(Mapping[str, Any], revision.get("navigation"))
            documents = cast(list[str], revision.get("affected_document_ids"))
            units = cast(list[str], revision.get("affected_unit_ids"))
            lineages = cast(list[str], revision.get("affected_lineage_ids"))
            self.check("revision_affects_metadata", bool(documents and units and lineages))
            if nav.get("document_id") and nav.get("previous_document_id") and units:
                old_unit = str(nav.get("previous_unit_id") or units[0])
                new_unit = str(nav.get("unit_id") or units[-1])
                navigation = Navigation(
                    job_id=self.ids["job_fault"],
                    revision_id=str(revision["id"]),
                    old_document_id=str(nav["previous_document_id"]),
                    new_document_id=str(nav["document_id"]),
                    old_unit_id=old_unit,
                    new_unit_id=new_unit,
                    lineage_id=str((nav.get("unit_id") and lineages[0]) or lineages[0]),
                )
                if nav.get("previous_unit_id") and nav.get("unit_id"):
                    return navigation, revision
                fallback = (navigation, revision)
        if fallback is None:
            self.fail("REQUIRED_REVISION_NAVIGATION_NOT_FOUND")
        return fallback

    async def validate_documents_and_diff(
        self, client: httpx.AsyncClient, navigation: Navigation
    ) -> Mapping[str, Any]:
        old_doc = cast(
            Mapping[str, Any],
            await self.require_get(
                client, f"{PREFIX}/documents/{navigation.old_document_id}?limit=100"
            ),
        )
        new_doc = cast(
            Mapping[str, Any],
            await self.require_get(
                client, f"{PREFIX}/documents/{navigation.new_document_id}?limit=100"
            ),
        )
        self.check("document_navigation_links", old_doc.get("next_document_id") is not None)
        self.check("document_previous_links", new_doc.get("previous_document_id") is not None)
        diff = cast(
            Mapping[str, Any],
            await self.require_get(
                client,
                f"{PREFIX}/documents/{navigation.new_document_id}/diff"
                f"?old_document_id={navigation.old_document_id}&limit=100",
            ),
        )
        counts = cast(Mapping[str, int], diff["counts"])
        changes = cast(Mapping[str, Any], diff["changes"])
        self.check("diff_document_pair_counts", sum(counts.values()) == int(changes["total"]))
        self.check("diff_has_change_items", int(changes["total"]) > 0)
        found_pair = False
        for change in cast(list[Mapping[str, Any]], changes["items"]):
            old = change.get("old")
            new = change.get("new")
            if isinstance(old, Mapping) and isinstance(new, Mapping):
                same_lineage = old.get("lineage_id") == new.get("lineage_id")
                found_pair = found_pair or same_lineage
                if (
                    old.get("id") == navigation.old_unit_id
                    or new.get("id") == navigation.new_unit_id
                ):
                    found_pair = same_lineage
                    break
        self.check("diff_old_new_unit_lineage_pair", found_pair)
        self.counts["diff_counts"] = dict(counts)
        self.ids.update(
            {
                "old_document": navigation.old_document_id,
                "new_document": navigation.new_document_id,
                "old_unit": navigation.old_unit_id,
                "new_unit": navigation.new_unit_id,
                "lineage": navigation.lineage_id,
                "revision": navigation.revision_id,
            }
        )
        return diff

    async def validate_evidence_and_history(
        self, client: httpx.AsyncClient, navigation: Navigation
    ) -> None:
        lineage = cast(
            Mapping[str, Any],
            await self.require_get(client, f"{PREFIX}/lineages/{navigation.lineage_id}?limit=100"),
        )
        versions = cast(
            list[Mapping[str, Any]], cast(Mapping[str, Any], lineage["versions"])["items"]
        )
        self.check("lineage_has_history", len(versions) >= 2)
        evidence_texts: list[str] = []
        evidence_units = [navigation.old_unit_id, navigation.new_unit_id]
        if len(versions) >= 3:
            evidence_units = [
                str(versions[0]["id"]),
                str(versions[1]["id"]),
                str(versions[-1]["id"]),
            ]
        for index, unit_id in enumerate(dict.fromkeys(evidence_units)):
            evidence = cast(
                Mapping[str, Any],
                await self.require_get(client, f"{PREFIX}/units/{unit_id}/evidence", evidence=True),
            )
            unit = cast(
                Mapping[str, Any], await self.require_get(client, f"{PREFIX}/units/{unit_id}")
            )
            text = str(evidence.get("text"))
            evidence_texts.append(text)
            self.check(f"evidence_{index}_exact_unit", evidence.get("unit_id") == unit_id)
            self.check(
                f"evidence_{index}_exact_document",
                evidence.get("document_id") == unit.get("document_id"),
            )
            self.check(
                f"evidence_{index}_range", int(evidence["end_char"]) > int(evidence["start_char"])
            )
            self.check(f"evidence_{index}_checksum", evidence.get("document_checksum") is not None)
            self.check(f"evidence_{index}_not_empty", bool(text.strip()))
        if len(evidence_texts) >= 2:
            self.check("historical_evidence_not_latest_only", len(set(evidence_texts)) > 1)
        self.counts["lineage_version_count"] = len(versions)
        self.counts["evidence_checked_count"] = len(set(evidence_units))

    def find_cross_source_old_document(self, new_document_id: str) -> str:
        with self.sqlite_connect() as connection:
            current = connection.execute(
                "SELECT source_id FROM documents WHERE id = ?", (UUID(new_document_id).hex,)
            ).fetchone()
            self.check("new_document_exists_in_db", current is not None)
            other = connection.execute(
                "SELECT id FROM documents WHERE source_id != ? "
                "ORDER BY ingested_at DESC, id DESC LIMIT 1",
                (current["source_id"],),
            ).fetchone()
            self.check("alternate_document_available", other is not None, "ALT_DOC_NOT_FOUND")
            return self.uuid_text(other["id"])

    async def validate_errors(self, client: httpx.AsyncClient, navigation: Navigation) -> None:
        unknown = str(uuid4())
        checks = {
            "unknown_uuid_404": await self.call(client, "GET", f"{PREFIX}/jobs/{unknown}"),
            "invalid_param_422": await self.call(client, "GET", f"{PREFIX}/jobs?limit=0"),
            "post_405": await self.call(client, "POST", f"{PREFIX}/jobs"),
        }
        other_doc = self.find_cross_source_old_document(navigation.new_document_id)
        checks["disjoint_document_409"] = await self.call(
            client,
            "GET",
            f"{PREFIX}/documents/{navigation.new_document_id}/diff?old_document_id={other_doc}",
        )
        expected = {
            "unknown_uuid_404": 404,
            "invalid_param_422": 422,
            "disjoint_document_409": 409,
            "post_405": 405,
        }
        for name, result in checks.items():
            self.check(name, result.status_code == expected[name])
            self.assert_safe_response(result)

    async def run_http(self, library: KnowledgeLibrary) -> None:
        client = await self.verify_enablement(library)
        try:
            jobs = cast(Mapping[str, Any], await self.require_get(client, f"{PREFIX}/jobs?limit=3"))
            _items, mapping = self.validate_jobs(jobs)
            failed_job = await self.validate_job_detail(client, mapping["fault"])
            revisions = cast(list[str], failed_job["trigger_revision_ids"])
            navigation, _revision = await self.find_navigation(client, revisions)
            await self.validate_documents_and_diff(client, navigation)
            await self.validate_evidence_and_history(client, navigation)
            await self.validate_errors(client, navigation)
        finally:
            await client.aclose()

    def payload(self, status: str, error_code: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "purpose": "realacceptance-b1-management-http-only",
            "database": {"kind": "saved-real-knowledge-acceptance"},
            "job_statuses_created_at_desc": self.statuses,
            "mapping": {
                "list_index_0": "restore",
                "list_index_1": "fault",
                "list_index_2": "positive",
            },
            "counts": self.counts,
            "metrics": self.metrics,
            "ids": self.ids,
            "routes": self.routes,
            "checks": self.checks,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        return payload

    def write_report(self, payload: Mapping[str, Any]) -> None:
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.safe_report_value(payload)
        with self.report.open("x", encoding="utf-8") as stream:
            json.dump(
                payload, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            stream.write("\n")

    def run(self) -> int:
        try:
            self.validate_inputs()
            before = self.snapshot()
            self.counts["storage_before"] = self.public_counts(before["row_counts"])
            engine, library = self.open_library()
            try:
                asyncio.run(self.run_http(library))
            finally:
                engine.dispose()
            after = self.snapshot()
            self.check("sqlite_digest_unchanged", before["digest"] == after["digest"])
            self.check("sqlite_row_counts_unchanged", before["row_counts"] == after["row_counts"])
            self.check(
                "no_job_created",
                before["row_counts"].get("auto_evaluation_jobs")
                == after["row_counts"].get("auto_evaluation_jobs"),
            )
            self.counts["storage_after"] = self.public_counts(after["row_counts"])
            payload = self.payload("passed")
            self.write_report(payload)
            print(json.dumps({"status": "passed", "report": str(self.report)}, ensure_ascii=False))
            return 0
        except Exception as error:
            code = (
                str(error) if isinstance(error, AcceptanceError) else type(error).__name__.upper()
            )
            if not code.replace("_", "").isalnum():
                code = "RUNNER_ERROR"
            payload = self.payload("failed", code)
            try:
                self.write_report(payload)
                report_value: str | None = str(self.report)
            except FileExistsError:
                report_value = None
                code = "REPORT_EXISTS"
            print(
                json.dumps(
                    {"status": "failed", "report": report_value, "error_code": code},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="scripts/real_knowledge_acceptance.py 生成的 knowledge.db（必填，不自动建库）。",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="safe 摘要报告；exclusive 创建，默认拒绝覆盖。",
    )
    args = parser.parse_args(argv)
    return Runner(args.database, args.report).run()


if __name__ == "__main__":
    raise SystemExit(main())
