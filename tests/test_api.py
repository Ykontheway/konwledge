from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from konwledge.api.app import create_app
from konwledge.application import KnowledgeLibrary
from konwledge.context import ContextPacket
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine


@pytest.fixture
def library() -> Iterator[KnowledgeLibrary]:
    engine = create_sqlite_engine("sqlite:///:memory:")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    try:
        yield KnowledgeLibrary(session_factory)
    finally:
        engine.dispose()


@pytest.fixture
def client(library: KnowledgeLibrary) -> Iterator[TestClient]:
    app = create_app(library, api_key="test-key")
    with TestClient(app) as test_client:
        yield test_client


def test_health_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_protection(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-key"}

    # Unauthorized
    assert client.get("/v1/models").status_code == 401
    assert client.post("/v1/context", json={"query": "q"}).status_code == 401

    # Authorized
    assert client.get("/v1/models", headers=headers).status_code == 200


def test_models_list(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-key"}
    response = client.get("/v1/models", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert data["data"][0]["id"] == "knowledge-context"


def test_context_basic_retrieval(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    # Import some knowledge
    doc = tmp_path / "test.md"
    doc.write_text("Unique knowledge content for testing.", encoding="utf-8")
    library.import_path(doc)

    headers = {"Authorization": "Bearer test-key"}
    response = client.post("/v1/context", headers=headers, json={"query": "Unique"})
    assert response.status_code == 200
    packet = response.json()
    assert packet["query"] == "Unique"
    assert len(packet["blocks"]) == 1
    assert "Unique knowledge" in packet["context_text"]


def test_context_strict_validation(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-key"}

    # top_k too large
    response = client.post("/v1/context", headers=headers, json={"query": "q", "top_k": 101})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["param"] == "top_k"

    # Extra fields rejected
    response = client.post("/v1/context", headers=headers, json={"query": "q", "unknown": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_option"

    # Multiple source_ids rejected
    response = client.post(
        "/v1/context",
        headers=headers,
        json={"query": "q", "source_ids": [str(uuid4()), str(uuid4())]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "source_ids"


def test_context_lifecycle_filters(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    doc = tmp_path / "lifecycle.md"
    doc.write_text("Lifecycle testing.", encoding="utf-8")
    report = library.import_path(doc)
    unit_id = report.imported[0].result.units[0].id

    headers = {"Authorization": "Bearer test-key"}

    # Active
    active = client.post("/v1/context", headers=headers, json={"query": "Lifecycle"})
    assert len(active.json()["blocks"]) == 1

    # Stale
    library.lifecycle.mark_stale(unit_id, reason="testing stale")
    assert (
        len(
            client.post("/v1/context", headers=headers, json={"query": "Lifecycle"}).json()[
                "blocks"
            ]
        )
        == 0
    )

    # Include Stale
    response = client.post(
        "/v1/context", headers=headers, json={"query": "Lifecycle", "include_stale": True}
    )
    assert len(response.json()["blocks"]) == 1
    assert response.json()["blocks"][0]["status"] == "stale"


def test_chat_completions_openai_envelope(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    doc = tmp_path / "chat.md"
    doc.write_text("Chat knowledge content.", encoding="utf-8")
    library.import_path(doc)

    headers = {"Authorization": "Bearer test-key"}
    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "knowledge-context",
            "messages": [{"role": "user", "content": "chat knowledge"}],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "knowledge-context"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in data

    content_json = data["choices"][0]["message"]["content"]
    assert "\n" not in content_json
    packet = ContextPacket.model_validate_json(content_json)
    assert packet.query == "chat knowledge"
    assert len(packet.blocks) == 1


def test_chat_completions_with_overrides(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    doc = tmp_path / "override.md"
    doc.write_text("Override testing.", encoding="utf-8")
    library.import_path(doc)

    headers = {"Authorization": "Bearer test-key"}
    # Override top_k to 0 (invalid) or use max_chars to force skip
    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "knowledge-context",
            "messages": [{"role": "user", "content": "Override"}],
            "context": {"max_chars": 0},
        },
    )
    assert response.status_code == 200
    content = json.loads(response.json()["choices"][0]["message"]["content"])
    assert len(content["blocks"]) == 0
    assert content["skipped"][0]["reason"] == "budget"


def test_serialization_concurrency(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    # We can verify serialization by ensuring only one library operation happens
    # at a time using SQLAlchemy event listeners to track concurrent sessions.
    doc = tmp_path / "concurrent.md"
    doc.write_text("Concurrent content.", encoding="utf-8")
    library.import_path(doc)

    headers = {"Authorization": "Bearer test-key"}
    session_starts = 0
    max_concurrent_sessions = 0
    concurrency_lock = threading.Lock()

    def before_begin(session: Any, transaction: Any, connection: Any) -> None:
        nonlocal session_starts, max_concurrent_sessions
        with concurrency_lock:
            session_starts += 1
            max_concurrent_sessions = max(max_concurrent_sessions, session_starts)

    def after_transaction_end(session: Any, transaction: Any) -> None:
        nonlocal session_starts
        with concurrency_lock:
            session_starts -= 1

    event.listen(library._session_factory, "after_begin", before_begin)
    event.listen(library._session_factory, "after_transaction_end", after_transaction_end)

    results = []

    def make_request() -> None:
        resp = client.post("/v1/context", headers=headers, json={"query": "Concurrent"})
        results.append(resp.status_code)

    threads = [threading.Thread(target=make_request) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Even with concurrent HTTP requests, the RLock ensures sequential library UoW.
    assert max_concurrent_sessions == 1
    assert all(code == 200 for code in results)


def test_vector_unsupported_error(library: KnowledgeLibrary, client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-key"}
    response = client.post("/v1/context", headers=headers, json={"query": "q", "mode": "vector"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "retrieval_mode_unavailable"


def test_as_of_aware_requirement(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-key"}

    # Naive timestamp rejected by Pydantic AwareDatetime
    response = client.post(
        "/v1/context", headers=headers, json={"query": "q", "as_of": "2026-11-11T12:00:00"}
    )
    assert response.status_code == 400
    assert "validation_error" in response.json()["error"]["code"]

    # Aware timestamp accepted
    response = client.post(
        "/v1/context", headers=headers, json={"query": "q", "as_of": "2026-11-11T12:00:00Z"}
    )
    assert response.status_code == 200


def test_native_and_chat_return_same_packet_with_fixed_as_of(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    doc = tmp_path / "same.md"
    doc.write_text("Same packet evidence.", encoding="utf-8")
    library.import_path(doc)
    headers = {"Authorization": "Bearer test-key"}
    as_of = "2030-01-01T00:00:00Z"

    native = client.post(
        "/v1/context",
        headers=headers,
        json={"query": "Same packet", "as_of": as_of},
    )
    chat = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "knowledge-context",
            "messages": [{"role": "user", "content": "Same packet"}],
            "context": {"as_of": as_of},
        },
    )

    assert native.status_code == chat.status_code == 200
    chat_packet = json.loads(chat.json()["choices"][0]["message"]["content"])
    assert chat_packet == native.json()


def test_source_filter_and_empty_context(
    library: KnowledgeLibrary, client: TestClient, tmp_path: Path
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("Sourcefilter first evidence.", encoding="utf-8")
    second.write_text("Sourcefilter second evidence.", encoding="utf-8")
    first_report = library.import_path(first)
    library.import_path(second)
    source_id = first_report.imported[0].result.ingestion.document.source_id
    headers = {"Authorization": "Bearer test-key"}

    filtered = client.post(
        "/v1/context",
        headers=headers,
        json={"query": "Sourcefilter", "source_ids": [str(source_id)]},
    )
    empty = client.post(
        "/v1/context",
        headers=headers,
        json={"query": "Sourcefilter", "source_ids": [str(uuid4())]},
    )

    assert filtered.status_code == empty.status_code == 200
    assert len(filtered.json()["blocks"]) == 1
    assert filtered.json()["blocks"][0]["citation"]["source_id"] == str(source_id)
    assert empty.json()["blocks"] == []
    assert empty.json()["context_text"] == ""


@pytest.mark.parametrize(
    ("changes", "param"),
    [
        ({"top_k": True}, "top_k"),
        ({"top_k": 101}, "top_k"),
        ({"candidate_limit": 1001}, "candidate_limit"),
        ({"top_k": 10, "candidate_limit": 9}, None),
        ({"max_chars": 100_001}, "max_chars"),
        ({"max_units": 101}, "max_units"),
        ({"include_stale": 1}, "include_stale"),
        ({"as_of": "2026-11-11T12:00:00"}, "as_of"),
        ({"query": "x" * 100_001}, None),
    ],
)
def test_context_resource_constraints(
    client: TestClient, changes: dict[str, Any], param: str | None
) -> None:
    headers = {"Authorization": "Bearer test-key"}
    response = client.post("/v1/context", headers=headers, json={"query": "q", **changes})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    if param is not None:
        assert error["param"] == param


@pytest.mark.parametrize(
    "changes",
    [
        {"stream": True},
        {"temperature": 0},
        {"model": "other"},
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "q"}]},
        {
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "user", "content": "again"},
            ]
        },
    ],
)
def test_chat_rejects_unsupported_options(client: TestClient, changes: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "model": "knowledge-context",
        "messages": [{"role": "user", "content": "q"}],
        **changes,
    }
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json=payload,
    )

    assert response.status_code == 400
    assert set(response.json()) == {"error"}


def test_openapi_schema_and_authenticated_docs_policy(library: KnowledgeLibrary) -> None:
    public_app = create_app(library)
    schema = public_app.openapi()

    assert "ContextRequest" in schema["components"]["schemas"]
    assert "ContextPacket" in schema["components"]["schemas"]
    context_operation = schema["paths"]["/v1/context"]["post"]
    response_schema = context_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["$ref"].endswith("/ContextPacket")
    assert "security" not in context_operation

    with TestClient(public_app) as public_client:
        assert (
            public_client.get("/v1/models", headers={"Authorization": "not-bearer"}).status_code
            == 200
        )

    with TestClient(create_app(library, api_key="secret")) as protected:
        assert protected.get("/health").status_code == 200
        assert protected.get("/docs").status_code == 404
        assert protected.get("/openapi.json").status_code == 404
    with pytest.raises(ValueError, match="must not be empty"):
        create_app(library, api_key="")


def test_context_route_performs_no_writes(tmp_path: Path) -> None:
    engine = create_sqlite_engine("sqlite:///:memory:")
    create_schema(engine)
    library = KnowledgeLibrary(create_session_factory(engine))
    doc = tmp_path / "readonly.md"
    doc.write_text("Read only evidence.", encoding="utf-8")
    library.import_path(doc)
    writes: list[str] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        operation = statement.lstrip().split(maxsplit=1)[0].upper()
        if operation in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with TestClient(create_app(library)) as no_auth_client:
            response = no_auth_client.post("/v1/context", json={"query": "Read only"})
        assert response.status_code == 200
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()


def test_internal_errors_are_safe_and_do_not_reflect_private_data() -> None:
    private = "secret prompt SQL /private/path"

    class FailingLibrary:
        def build_context(self, *args: Any, **kwargs: Any) -> ContextPacket:
            raise RuntimeError(private)

    app = create_app(cast(KnowledgeLibrary, FailingLibrary()))
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = failing_client.post("/v1/context", json={"query": private})

    assert response.status_code == 500
    body = response.text
    assert private not in body
    assert "traceback" not in body.casefold()
    assert set(response.json()) == {"error"}
