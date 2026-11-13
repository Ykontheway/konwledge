"""B1 管理契约: 真实持久化导航、白名单安全以及无写入读取。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, delete, event, insert, select, text, update
from sqlalchemy.exc import OperationalError

from konwledge.api import management_models as models
from konwledge.api.app import create_app
from konwledge.api.management import build_management_router
from konwledge.api.management_service import ManagementService
from konwledge.application import KnowledgeLibrary
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine
from konwledge.storage.tables import (
    AutoEvaluationJobRow,
    Base,
    DocumentRow,
    KnowledgeUnitRow,
    RevisionRow,
)

PREFIX = "/v1/management"
SECRET = "private_query_DSN_author_reviewer_rawpayload_7d28"
OLD = "# Rules\n保持本版本的原文。\n\n# Quota\n限额为 10。\n"
NEW = "# Rules\n保持本版本的原文。\n\n# Quota\n限额为 20。\n\n# Extra\n新增规则。\n"


def client_for(library: KnowledgeLibrary) -> TestClient:
    app = FastAPI()
    app.include_router(build_management_router(library))
    return TestClient(app, raise_server_exceptions=False)


@dataclass
class Fixture:
    library: KnowledgeLibrary
    engine: Engine
    client: TestClient
    old_id: UUID
    new_id: UUID
    other_id: UUID
    old_units: tuple[UUID, ...]
    new_units: tuple[UUID, ...]
    jobs: tuple[UUID, ...]
    revisions: tuple[UUID, ...]


@pytest.fixture(params=["file", "memory"])
def store(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Fixture]:
    url = (
        "sqlite+pysqlite:///:memory:"
        if request.param == "memory"
        else (f"sqlite+pysqlite:///{tmp_path / 'management.db'}")
    )
    engine = create_sqlite_engine(url)
    create_schema(engine)
    library = KnowledgeLibrary(create_session_factory(engine))
    source = tmp_path / "source.md"
    source.write_text(OLD, encoding="utf-8")
    first = library.import_path(source).imported[0].result
    source.write_text(NEW, encoding="utf-8")
    second = library.import_path(source).imported[0].result
    other = tmp_path / "other.md"
    other.write_text("# Other\n无关来源。\n", encoding="utf-8")
    unrelated = library.import_path(other).imported[0].result
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        revisions = tuple(connection.scalars(select(RevisionRow.id)).all())
        job_ids = tuple(uuid4() for _ in range(3))
        for identifier, status in zip(job_ids, ("queued", "failed", "passed"), strict=True):
            connection.execute(
                insert(AutoEvaluationJobRow).values(
                    id=identifier,
                    config_generation=1,
                    config_fingerprint="a" * 64,
                    dataset_id=UUID(int=123),
                    dataset_version=2,
                    dataset_hash="b" * 64,
                    knowledge_fingerprint="c" * 64,
                    trigger_revision_ids=list(map(str, revisions)),
                    status=status,
                    created_at=now,
                    evaluated_at=now,
                    attempt_count=0,
                    recovery_errors=[{"code": SECRET, "raw": SECRET}],
                    error_code="QUALITY_GATE_FAILED" if status == "failed" else None,
                    report={
                        "passed": status != "failed",
                        "metrics": {
                            "case_count": 4,
                            "answerable_count": 3,
                            "no_answer_count": 1,
                            "mean_recall": 0.8,
                            "mean_precision": 0.5,
                            "mean_reciprocal_rank": 0.75,
                            "mean_ndcg": 0.8,
                            "hit_rate": 1.0,
                            "no_answer_accuracy": 1.0,
                            "query": SECRET,
                        },
                        "citation_coverage": 1.0,
                        "stale_hits": 0,
                        "degraded": False,
                        "checks": [
                            {
                                "name": "recall_at_k",
                                "passed": True,
                                "measured": 0.8,
                                "required": ">=0.8",
                                "detail": SECRET,
                            },
                            {
                                "name": "regression_gate",
                                "passed": False,
                                "measured": SECRET,
                                "required": SECRET,
                            },
                            {"name": SECRET, "passed": True, "measured": 1, "required": SECRET},
                        ],
                        "query": SECRET,
                        "rawpayload": SECRET,
                        "baseline": {"query": SECRET},
                    },
                )
            )
        for row in connection.execute(select(RevisionRow.id, RevisionRow.payload)):
            payload = {
                **row.payload,
                "metadata": {"query": SECRET},
                "text": SECRET,
                "locator": SECRET,
                "author": SECRET,
                "reviewer": SECRET,
            }
            connection.execute(
                update(RevisionRow)
                .where(RevisionRow.id == row.id)
                .values(author=SECRET, payload=payload)
            )
        connection.execute(update(DocumentRow).values(metadata_json={"rawpayload": SECRET}))
    client = client_for(library)
    try:
        yield Fixture(
            library,
            engine,
            client,
            first.ingestion.document.id,
            second.ingestion.document.id,
            unrelated.ingestion.document.id,
            tuple(unit.id for unit in first.units),
            tuple(unit.id for unit in second.units),
            job_ids,
            revisions,
        )
    finally:
        client.close()
        engine.dispose()


def snapshot(engine: Engine) -> str:
    with engine.connect() as connection:
        values = {
            table.name: sorted(
                (dict(row) for row in connection.execute(select(table)).mappings()),
                key=lambda row: json.dumps(row, default=str, sort_keys=True),
            )
            for table in Base.metadata.sorted_tables
        }
    return json.dumps(values, default=str, sort_keys=True)


def paths(store: Fixture) -> list[str]:
    lineage = store.library.unit_history(store.old_units[0]).lineage_id
    return [
        f"{PREFIX}/jobs",
        f"{PREFIX}/jobs/{store.jobs[1]}",
        f"{PREFIX}/revisions/{store.revisions[0]}",
        f"{PREFIX}/lineages/{lineage}",
        f"{PREFIX}/documents/{store.old_id}",
        f"{PREFIX}/units/{store.old_units[0]}",
        f"{PREFIX}/documents/{store.new_id}/diff?old_document_id={store.old_id}",
        f"{PREFIX}/units/{store.old_units[1]}/evidence",
    ]


def test_routes_and_openapi_are_get_only(store: Fixture) -> None:
    router = build_management_router(store.library)
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 8
    assert all(route.methods == {"GET"} for route in routes)
    assert all(route.path.startswith(PREFIX) for route in routes)
    app = store.client.app
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    assert len(schema["paths"]) == 8
    for name, definition in schema["components"]["schemas"].items():
        if (
            name
            not in {
                "APIErrorDetail",
                "APIErrorResponse",
                "HTTPValidationError",
                "ValidationError",
            }
            and "properties" in definition
        ):
            assert definition["additionalProperties"] is False


def test_models_frozen_and_extra_forbidden_recursively(store: Fixture) -> None:
    job = models.JobDTO.model_validate(store.client.get(f"{PREFIX}/jobs/{store.jobs[1]}").json())
    assert job.report is not None
    for value in (job, job.report, job.report.metrics, job.report.checks[0], job.links[0]):
        assert value.model_config["frozen"] is True
        assert value.model_config["extra"] == "forbid"
        with pytest.raises(ValidationError):
            setattr(value, next(iter(type(value).model_fields)), "changed")
        with pytest.raises(ValidationError):
            type(value).model_validate({**value.model_dump(), "rawpayload": SECRET})
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, models.ManagementModel):
            assert value.model_config["frozen"] is True
            assert value.model_config["extra"] == "forbid"


def test_jobs_pagination_and_filters(store: Fixture) -> None:
    default = store.client.get(f"{PREFIX}/jobs").json()
    assert default["total"] == 3 and default["limit"] == 20 and default["offset"] == 0
    pages = [
        store.client.get(f"{PREFIX}/jobs?limit=1&offset={offset}").json() for offset in range(4)
    ]
    assert len({page["items"][0]["id"] for page in pages[:3]}) == 3
    assert pages[-1]["items"] == []
    failed = store.client.get(
        f"{PREFIX}/jobs?status=failed&dataset_id={UUID(int=123)}&dataset_version=2"
    ).json()
    assert failed["total"] == 1 and failed["items"][0]["id"] == str(store.jobs[1])
    for query in (
        "status=blocked",
        "dataset_version=3",
        f"dataset_id={uuid4()}",
        f"parent_job_id={uuid4()}",
    ):
        assert store.client.get(f"{PREFIX}/jobs?{query}").json()["total"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=1.5",
        "offset=-1",
        "offset=100001",
        "status=unknown",
        f"status={SECRET}",
        f"dataset_id={SECRET}",
        "dataset_version=0",
        f"parent_job_id={SECRET}",
        f"query={SECRET}",
    ],
)
def test_invalid_job_queries_safe_422(store: Fixture, query: str) -> None:
    response = store.client.get(f"{PREFIX}/jobs?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "management_validation_error"
    assert SECRET not in response.text
    assert "input" not in response.text


@pytest.mark.parametrize("collection", ["jobs", "revisions", "lineages", "documents", "units"])
def test_unknown_and_invalid_ids(store: Fixture, collection: str) -> None:
    assert store.client.get(f"{PREFIX}/{collection}/{uuid4()}").status_code == 404
    response = store.client.get(f"{PREFIX}/{collection}/{SECRET}")
    assert response.status_code == 422 and SECRET not in response.text


@pytest.mark.parametrize("resource", ["lineages", "documents"])
@pytest.mark.parametrize("query", ["limit=101", "offset=100001", f"rawpayload={SECRET}"])
def test_other_pagination_validation(store: Fixture, resource: str, query: str) -> None:
    identifier = store.old_id if resource == "documents" else store.old_units[0]
    response = store.client.get(f"{PREFIX}/{resource}/{identifier}?{query}")
    assert response.status_code == 422 and SECRET not in response.text


def test_safe_projection_no_report_payload_source_leak(store: Fixture) -> None:
    for path in paths(store):
        response = store.client.get(path)
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
        assert str(store.engine.url) not in response.text
        assert "locator" not in response.text and "metadata" not in response.text
        assert "rawpayload" not in response.text and "reviewer" not in response.text
        if not path.endswith("/evidence"):
            assert '"text"' not in response.text
            assert '"query"' not in response.text
            assert OLD not in response.text and NEW not in response.text
    job = store.client.get(f"{PREFIX}/jobs/{store.jobs[1]}").json()
    assert job["report"]["metrics"]["case_count"] == 4
    assert job["report"]["checks"] == [
        {"name": "recall_at_k", "passed": True, "actual": 0.8, "threshold": ">=0.8"},
        {"name": "regression_gate", "passed": False, "actual": None, "threshold": None},
    ]
    assert job["error_code"] == "QUALITY_GATE_FAILED"
    with store.engine.begin() as connection:
        connection.execute(
            update(AutoEvaluationJobRow)
            .where(AutoEvaluationJobRow.id == store.jobs[1])
            .values(error_code=SECRET)
        )
    assert (
        store.client.get(f"{PREFIX}/jobs/{store.jobs[1]}").json()["error_code"] == "UNKNOWN_ERROR"
    )


def test_revision_ids_resolve_real_documents_units_lineages(store: Fixture) -> None:
    job = store.client.get(f"{PREFIX}/jobs/{store.jobs[1]}").json()
    assert set(job["trigger_revision_ids"]) == set(map(str, store.revisions))
    for revision_link in job["links"]:
        revision = store.client.get(revision_link["href"]).json()
        if revision["entity_type"] == "knowledge_unit":
            assert revision["entity_id"] in revision["affected_lineage_ids"]
        for identifier in revision["affected_document_ids"]:
            assert store.client.get(f"{PREFIX}/documents/{identifier}").status_code == 200
        for identifier in revision["affected_unit_ids"]:
            unit = store.client.get(f"{PREFIX}/units/{identifier}").json()
            assert unit["document_id"] in revision["affected_document_ids"]
            assert unit["lineage_id"] in revision["affected_lineage_ids"]
        for link in revision["links"]:
            assert store.client.get(link["href"]).status_code == 200


def test_diff_exact_lineage_and_evidence_links(store: Fixture) -> None:
    url = f"{PREFIX}/documents/{store.new_id}/diff?old_document_id={store.old_id}"
    result = store.client.get(url).json()
    assert (
        result["source_id"]
        == store.client.get(f"{PREFIX}/documents/{store.old_id}").json()["source_id"]
    )
    assert result["counts"] == {"added": 1, "modified": 1, "moved": 0, "unchanged": 1, "removed": 0}
    assert [change["kind"] for change in result["changes"]["items"]] == [
        "unchanged",
        "modified",
        "added",
    ]
    for offset in range(4):
        page = store.client.get(url + f"&limit=1&offset={offset}").json()["changes"]
        assert page["items"] == result["changes"]["items"][offset : offset + 1]
        assert page["total"] == 3
    reverse = store.client.get(
        f"{PREFIX}/documents/{store.old_id}/diff?old_document_id={store.new_id}"
    ).json()
    assert reverse["counts"]["removed"] == 1
    for change in result["changes"]["items"]:
        for side in (change["old"], change["new"]):
            if side:
                link = next(link for link in side["links"] if link["kind"] == "evidence")
                response = store.client.get(link["href"])
                assert response.status_code == 200, response.text
                evidence = response.json()
                assert evidence["document_id"] == side["document_id"]
                assert evidence["unit_id"] == side["id"]
                assert evidence["content_hash"] == side["content_hash"]
    same = store.client.get(
        f"{PREFIX}/documents/{store.old_id}/diff?old_document_id={store.old_id}"
    ).json()
    assert same["counts"]["unchanged"] == 2


@pytest.mark.parametrize(
    "query, status",
    [
        ("", 422),
        (f"old_document_id={SECRET}", 422),
        (f"old_document_id={uuid4()}", 404),
    ],
)
def test_diff_invalid_or_unknown_old(store: Fixture, query: str, status: int) -> None:
    response = store.client.get(f"{PREFIX}/documents/{store.new_id}/diff?{query}")
    assert response.status_code == status and SECRET not in response.text


def test_cross_source_diff_rejected(store: Fixture) -> None:
    response = store.client.get(
        f"{PREFIX}/documents/{store.new_id}/diff?old_document_id={store.other_id}"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "diff_source_mismatch"


def test_historical_body_not_latest_and_finite_navigation(store: Fixture) -> None:
    old_evidence = store.client.get(f"{PREFIX}/units/{store.old_units[1]}/evidence").json()
    new_evidence = store.client.get(f"{PREFIX}/units/{store.new_units[1]}/evidence").json()
    assert "10" in old_evidence["text"] and "20" not in old_evidence["text"]
    assert "20" in new_evidence["text"]
    assert old_evidence["document_id"] == str(store.old_id)
    assert old_evidence["text"] == OLD[old_evidence["start_char"] : old_evidence["end_char"]]
    assert old_evidence["revision_ids"]
    lineage = old_evidence["lineage_id"]
    page = store.client.get(f"{PREFIX}/lineages/{lineage}?limit=1&offset=1").json()
    assert page["versions"]["total"] == 2
    assert page["versions"]["items"][0]["document_id"] == str(store.new_id)
    old_document = store.client.get(f"{PREFIX}/documents/{store.old_id}?limit=1").json()
    assert old_document["next_document_id"] == str(store.new_id)
    assert len(old_document["units"]["items"]) == 1
    assert old_document["units"]["total"] == 2
    assert store.client.get(f"{PREFIX}/documents/{store.new_id}").json()[
        "previous_document_id"
    ] == str(store.old_id)


@pytest.mark.parametrize("corruption", ["text", "range", "checksum", "no_anchor", "raw_payload"])
def test_corrupt_or_unavailable_history_not_fabricated(store: Fixture, corruption: str) -> None:
    with store.engine.begin() as connection:
        if corruption == "text":
            row = connection.execute(
                select(KnowledgeUnitRow.text).where(KnowledgeUnitRow.id == store.old_units[1])
            ).one()
            connection.execute(
                update(KnowledgeUnitRow)
                .where(KnowledgeUnitRow.id == store.old_units[1])
                .values(text=row.text.replace("10", "99"))
            )
        elif corruption == "range":
            connection.execute(
                update(KnowledgeUnitRow)
                .where(KnowledgeUnitRow.id == store.old_units[1])
                .values(end_char=10000)
            )
        elif corruption == "checksum":
            connection.execute(
                update(DocumentRow).where(DocumentRow.id == store.old_id).values(checksum="d" * 64)
            )
        elif corruption == "raw_payload":
            connection.execute(update(RevisionRow).values(payload=[SECRET]))
        else:
            connection.execute(delete(RevisionRow))
    response = store.client.get(f"{PREFIX}/units/{store.old_units[1]}/evidence")
    assert response.status_code == (410 if corruption == "no_anchor" else 409)
    assert '"text"' not in response.text and SECRET not in response.text
    assert store.client.get(f"{PREFIX}/units/{uuid4()}/evidence").status_code == 404


def test_reads_no_rows_jobs_revisions_or_hook_changes(store: Fixture) -> None:
    before = snapshot(store.engine)
    commits: list[object] = []

    def record_commit(connection: object) -> None:
        commits.append(connection)

    event.listen(store.engine, "commit", record_commit)
    try:
        for path in paths(store):
            assert store.client.get(path).status_code == 200
        assert snapshot(store.engine) == before
        assert commits == []
        with store.engine.connect() as connection:
            assert connection.scalar(text("PRAGMA query_only")) == 0
        # 读取后仍可通过原 factory 正常写入, 不受连接 flag/hook 污染。
        marker = store.library.import_path(Path(store.library.list_documents()[0].source.uri))
        assert marker.imported
    finally:
        event.remove(store.engine, "commit", record_commit)


def test_query_only_blocks_sql_and_restores_original_pool(store: Fixture) -> None:
    service = ManagementService(store.library)
    with pytest.raises(OperationalError), service.read_session() as session:
        assert session.scalar(text("PRAGMA query_only")) == 1
        session.execute(update(AutoEvaluationJobRow).values(error_code="EVALUATION_ERROR"))
    with store.engine.begin() as connection:
        assert connection.scalar(text("PRAGMA query_only")) == 0
        connection.execute(
            update(AutoEvaluationJobRow)
            .where(AutoEvaluationJobRow.id == store.jobs[0])
            .values(error_code="EVALUATION_ERROR")
        )


def test_parallel_reads_do_not_poison_pool(store: Fixture) -> None:
    service = ManagementService(store.library)
    before = snapshot(store.engine)
    with ThreadPoolExecutor(max_workers=6) as executor:
        result = list(executor.map(lambda _index: service.job(store.jobs[0]), range(18)))
    assert all(job.id == store.jobs[0] for job in result)
    assert snapshot(store.engine) == before
    with store.engine.begin() as connection:
        assert connection.scalar(text("PRAGMA query_only")) == 0
        connection.execute(update(AutoEvaluationJobRow).values(error_code="EVALUATION_ERROR"))


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_methods_rejected(store: Fixture, method: str) -> None:
    before = snapshot(store.engine)
    for path in paths(store):
        response = store.client.request(method, path, json={"rawpayload": SECRET})
        assert response.status_code == 405 and SECRET not in response.text
    assert snapshot(store.engine) == before


def test_internal_error_only_safe_code(store: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_self: ManagementService, _identifier: UUID) -> models.JobDTO:
        raise RuntimeError(f"{SECRET}: sqlite+pysqlite:///absolute/path/password.db")

    monkeypatch.setattr(ManagementService, "job", fail)
    response = store.client.get(f"{PREFIX}/jobs/{store.jobs[0]}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "management_internal_error"
    assert SECRET not in response.text and "absolute/path" not in response.text


def test_create_app_management_integration_when_wired(store: Fixture) -> None:
    if "enable_management" not in inspect.signature(create_app).parameters:
        pytest.skip("app 接线由另一 agent 负责; standalone router 契约已验证")
    factory: Any = create_app
    key = "management-test-key"
    app = factory(store.library, enable_management=True, api_key=key)
    with TestClient(app) as client:
        assert client.get(f"{PREFIX}/jobs").status_code == 401
        assert (
            client.get(f"{PREFIX}/jobs", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        response = client.get(
            f"{PREFIX}/jobs", headers={"Authorization": "Bearer management-test-key"}
        )
        assert response.status_code == 200 and response.json()["total"] == 3
        response = client.get(
            f"{PREFIX}/jobs?status={SECRET}",
            headers={"Authorization": "Bearer management-test-key"},
        )
        assert response.status_code == 422 and SECRET not in response.text
    with TestClient(create_app(store.library, api_key=key)) as disabled:
        assert disabled.get(f"{PREFIX}/jobs").status_code == 404


def test_real_runner_passed_failed_passed_management_navigation(tmp_path: Path) -> None:
    from scripts.real_knowledge_acceptance import read_library, run_acceptance

    report_path = run_acceptance(tmp_path)
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed", report
    assert [step["job"]["status"] for step in report["steps"]] == ["passed", "failed", "passed"]
    with (
        read_library(report_path.parent / "knowledge.db") as library,
        client_for(library) as client,
    ):
        for step in report["steps"]:
            response = client.get(f"{PREFIX}/jobs/{step['job']['id']}")
            assert response.status_code == 200, response.text
            job = response.json()
            assert job["status"] == step["job"]["status"]
            assert job["trigger_revision_ids"]
            followed_evidence = 0
            for identifier in job["trigger_revision_ids"]:
                response = client.get(f"{PREFIX}/revisions/{identifier}")
                assert response.status_code == 200, response.text
                revision = response.json()
                for unit_id in revision["affected_unit_ids"]:
                    evidence = client.get(f"{PREFIX}/units/{unit_id}/evidence")
                    assert evidence.status_code == 200, evidence.text
                    assert evidence.json()["document_id"] in revision["affected_document_ids"]
                    followed_evidence += 1
                nav = revision["navigation"]
                if nav["document_id"] and nav["previous_document_id"]:
                    diff = client.get(
                        f"{PREFIX}/documents/{nav['document_id']}/diff"
                        f"?old_document_id={nav['previous_document_id']}"
                    )
                    assert diff.status_code == 200, diff.text
                    for change in diff.json()["changes"]["items"]:
                        for side in (change["old"], change["new"]):
                            if side:
                                evidence = client.get(f"{PREFIX}/units/{side['id']}/evidence")
                                assert evidence.status_code == 200, evidence.text
                                assert evidence.json()["document_id"] == side["document_id"]
            assert followed_evidence > 0


@pytest.mark.parametrize("missing", ["document", "source"])
def test_orphan_history_returns_safe_gone(store: Fixture, missing: str) -> None:
    from konwledge.storage.tables import SourceRow

    # 模拟历史库损坏, 仅在一次性测试库关闭 FK, finally 立即恢复。
    with store.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            if missing == "document":
                connection.execute(delete(DocumentRow).where(DocumentRow.id == store.old_id))
            else:
                source_id = connection.scalar(
                    select(DocumentRow.source_id).where(DocumentRow.id == store.old_id)
                )
                connection.execute(delete(SourceRow).where(SourceRow.id == source_id))
            connection.commit()
        finally:
            connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    response = store.client.get(f"{PREFIX}/units/{store.old_units[1]}/evidence")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == f"historical_{missing}_unavailable"
    assert '"text"' not in response.text


def test_reader_does_not_rollback_concurrent_writer(store: Fixture) -> None:
    service = ManagementService(store.library)
    with ThreadPoolExecutor(max_workers=1) as executor, store.engine.connect() as writer:
        writer.execute(
            update(AutoEvaluationJobRow)
            .where(AutoEvaluationJobRow.id == store.jobs[0])
            .values(error_code="EVALUATION_ERROR")
        )
        pending = executor.submit(service.job, store.jobs[0])
        # 若 reader 归还共享 StaticPool 导致 rollback, 本事务更新会在此消失。
        assert (
            writer.scalar(
                select(AutoEvaluationJobRow.error_code).where(
                    AutoEvaluationJobRow.id == store.jobs[0]
                )
            )
            == "EVALUATION_ERROR"
        )
        assert writer.scalar(text("PRAGMA query_only")) == 0
        writer.commit()
        assert pending.result(timeout=10).id == store.jobs[0]
    assert service.job(store.jobs[0]).error_code == "EVALUATION_ERROR"
