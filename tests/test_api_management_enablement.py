"""B1 管理 API opt-in 接线、安全默认值与 CLI 透传。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from typer.testing import CliRunner

from konwledge.api import server
from konwledge.api.app import create_app
from konwledge.application import KnowledgeLibrary
from konwledge.cli import app
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine
from konwledge.storage.tables import AutoEvaluationJobRow

KEY = "management-test-key"
MANAGEMENT_PATH = "/v1/management/jobs"

runner = CliRunner()


@pytest.fixture
def library() -> Iterator[KnowledgeLibrary]:
    engine = create_sqlite_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield KnowledgeLibrary(create_session_factory(engine))
    finally:
        engine.dispose()


def test_management_disabled_is_404_and_not_in_openapi(library: KnowledgeLibrary) -> None:
    application = create_app(library, api_key=KEY)
    with TestClient(application) as client:
        response = client.get(MANAGEMENT_PATH, headers={"Authorization": f"Bearer {KEY}"})
        assert response.status_code == 404
        schema = application.openapi()
    assert MANAGEMENT_PATH not in schema["paths"]
    assert "/v1/models" in schema["paths"]


@pytest.mark.parametrize("enable_management", ["true", 1, object()])
def test_create_app_rejects_non_bool_enable_management(
    library: KnowledgeLibrary, enable_management: object
) -> None:
    with pytest.raises(TypeError, match="enable_management"):
        create_app(library, api_key=KEY, enable_management=enable_management)  # type: ignore[arg-type]


@pytest.mark.parametrize("api_key", [None, "", " ", "\t\n"])
def test_management_enable_requires_nonblank_key_without_leakage(
    library: KnowledgeLibrary, api_key: str | None
) -> None:
    with pytest.raises(ValueError) as error:
        create_app(library, api_key=api_key, enable_management=True)
    text = str(error.value)
    assert "management" in text or "api_key" in text
    assert KEY not in text


def test_management_bearer_auth_and_openapi_when_enabled(library: KnowledgeLibrary) -> None:
    application = create_app(library, api_key=KEY, enable_management=True)
    with TestClient(application) as client:
        assert client.get(MANAGEMENT_PATH).status_code == 401
        wrong = client.get(MANAGEMENT_PATH, headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        response = client.get(MANAGEMENT_PATH, headers={"Authorization": f"Bearer {KEY}"})
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        schema = application.openapi()
    assert MANAGEMENT_PATH in schema["paths"]
    assert schema["paths"][MANAGEMENT_PATH]["get"]
    assert KEY not in json.dumps(schema)


def test_unauthenticated_management_request_does_not_touch_db(library: KnowledgeLibrary) -> None:
    application = create_app(library, api_key=KEY, enable_management=True)
    statements: list[str] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(library._session_factory.kw["bind"], "before_cursor_execute", capture_sql)
    with TestClient(application) as client:
        missing = client.get(MANAGEMENT_PATH)
        wrong = client.get(MANAGEMENT_PATH, headers={"Authorization": "Bearer wrong"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert statements == []
    assert KEY not in missing.text + wrong.text


def test_enable_management_get_jobs_is_readonly_and_does_not_create_jobs(tmp_path: Path) -> None:
    database = tmp_path / "library.db"
    engine = create_sqlite_engine(f"sqlite:///{database}")
    create_schema(engine)
    writes: list[str] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            writes.append(statement)

    try:
        library = KnowledgeLibrary(create_session_factory(engine))
        event.listen(engine, "before_cursor_execute", capture_sql)
        with TestClient(create_app(library, api_key=KEY, enable_management=True)) as client:
            response = client.get(MANAGEMENT_PATH, headers={"Authorization": f"Bearer {KEY}"})
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        with engine.connect() as connection:
            assert connection.scalar(select(AutoEvaluationJobRow.id).limit(1)) is None
        assert writes == []
    finally:
        engine.dispose()


def test_management_enablement_preserves_existing_u18_output(
    library: KnowledgeLibrary,
) -> None:
    without_management = create_app(library, api_key=KEY)
    with_management = create_app(library, api_key=KEY, enable_management=True)
    request = {"query": "empty", "top_k": 3, "max_units": 2}
    headers = {"Authorization": f"Bearer {KEY}"}
    with TestClient(without_management) as old_client, TestClient(with_management) as new_client:
        old_context = old_client.post("/v1/context", headers=headers, json=request)
        new_context = new_client.post("/v1/context", headers=headers, json=request)
        old_models = old_client.get("/v1/models", headers=headers)
        new_models = new_client.get("/v1/models", headers=headers)
    assert old_context.status_code == new_context.status_code == 200
    old_payload = old_context.json()
    new_payload = new_context.json()
    old_payload.pop("evaluated_at")
    new_payload.pop("evaluated_at")
    assert old_payload == new_payload
    assert old_models.status_code == new_models.status_code == 200
    assert old_models.json() == new_models.json()


def test_run_server_enable_management_validates_before_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    opened = Mock(side_effect=AssertionError("must not open storage"))
    monkeypatch.setattr(server, "create_sqlite_engine", opened)
    database = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="KNOWLEDGE_API_KEY"):
        server.run_server(database, enable_management=True)
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_run_server_rejects_non_bool_enable_management_before_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    opened = Mock(side_effect=AssertionError("must not open storage"))
    monkeypatch.setattr(server, "create_sqlite_engine", opened)
    with pytest.raises(TypeError, match="enable_management"):
        server.run_server(database, enable_management="yes")  # type: ignore[arg-type]
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == [database]


def test_run_server_management_preserves_loopback_existing_db_and_access_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    monkeypatch.setenv("KNOWLEDGE_API_KEY", KEY)
    engines: list[Engine] = []
    disposed = Mock()

    def capture_engine(url: str) -> Engine:
        assert "mode=rw&uri=true" in url
        opened = create_sqlite_engine(url)
        original = opened.dispose

        def dispose(close: bool = True) -> None:
            disposed()
            original(close=close)

        opened.dispose = dispose  # type: ignore[method-assign]
        engines.append(opened)
        return opened

    def run(application: Any, **kwargs: Any) -> None:
        assert kwargs == {"host": "127.0.0.1", "port": 8000, "access_log": False}
        with TestClient(application) as client:
            assert client.get(MANAGEMENT_PATH).status_code == 401
            assert (
                client.get(MANAGEMENT_PATH, headers={"Authorization": f"Bearer {KEY}"}).status_code
                == 200
            )

    monkeypatch.setattr(server, "create_sqlite_engine", capture_engine)
    monkeypatch.setattr(uvicorn, "run", run)
    server.run_server(database, host="localhost", enable_management=True)
    assert len(engines) == 1
    disposed.assert_called_once()
    assert sorted(path.name for path in tmp_path.iterdir()) == [database.name]


def test_cli_enable_management_flag_is_explicit_and_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    called = Mock()

    def run_server(database_arg: Path, **kwargs: Any) -> None:
        called(database_arg, **kwargs)

    monkeypatch.setattr(server, "run_server", run_server)
    result = runner.invoke(app, ["serve", "--database", str(database), "--enable-management"])
    assert result.exit_code == 0, result.output
    called.assert_called_once_with(
        database,
        host="127.0.0.1",
        port=8000,
        enable_management=True,
    )


def test_cli_default_management_disabled_and_help_without_api_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    called = Mock()

    def run_server(database_arg: Path, **kwargs: Any) -> None:
        called(database_arg, **kwargs)

    monkeypatch.setattr(server, "run_server", run_server)
    result = runner.invoke(app, ["serve", "--database", "existing.db"])
    assert result.exit_code == 0, result.output
    called.assert_called_once()
    assert called.call_args.kwargs["enable_management"] is False

    help_result = runner.invoke(app, ["serve", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--enable-management" in help_result.output
    assert list(tmp_path.iterdir()) == []
