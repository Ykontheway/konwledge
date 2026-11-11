"""验证启动边界；所有 uvicorn.run 调用均替换为同步测试桩。"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from typer.testing import CliRunner

from konwledge.api import server
from konwledge.cli import app
from konwledge.storage import create_sqlite_engine

runner = CliRunner()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("port", [1, 8000, 65535])
def test_serve_existing_database_and_dispose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, port: int
) -> None:
    # 包含 URI 特殊字符，验证 SQLite mode=rw URL 不会指向错误文件。
    database = tmp_path / "知识 ?#%.db"
    database.touch()
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "test-secret")
    engines: list[Engine] = []
    disposed = Mock()

    def capture_engine(url: str) -> Engine:
        assert "mode=rw&uri=true" in url
        engine = create_sqlite_engine(url)
        original_dispose = engine.dispose

        def dispose() -> None:
            disposed()
            original_dispose()

        monkeypatch.setattr(engine, "dispose", dispose)
        engines.append(engine)
        return engine

    def run(application: Any, **kwargs: Any) -> None:
        assert kwargs == {
            "host": "127.0.0.1" if host == "localhost" else host,
            "port": port,
            "access_log": False,
        }
        with TestClient(application) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/v1/models").status_code == 401
            response = client.post(
                "/v1/context",
                json={"query": "empty"},
                headers={"Authorization": "Bearer test-secret"},
            )
            assert response.status_code == 200
            assert response.json()["blocks"] == []

    monkeypatch.setattr(server, "create_sqlite_engine", capture_engine)
    monkeypatch.setattr(uvicorn, "run", run)
    result = runner.invoke(
        app, ["serve", "--database", str(database), "--host", host, "--port", str(port)]
    )
    assert result.exit_code == 0, result.output
    assert len(engines) == 1
    disposed.assert_called_once()
    assert "test-secret" not in result.output
    assert sorted(path.name for path in tmp_path.iterdir()) == [database.name]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "0.0.0.0"],
        ["--host", "example.com"],
        ["--host", "127.0.0.2"],
        ["--host", "[::1]"],
        ["--port", "0"],
        ["--port", "65536"],
        ["--port", "abc"],
        [],
    ],
)
def test_invalid_config_never_opens_or_creates_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    opened = Mock(side_effect=AssertionError("must not open database"))
    monkeypatch.setattr(server, "create_sqlite_engine", opened)
    result = runner.invoke(app, ["serve", *arguments])
    assert result.exit_code == 2, result.output
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["", " ", "\t\n"])
def test_explicit_blank_key_is_invalid_before_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_API_KEY", key)
    opened = Mock()
    monkeypatch.setattr(server, "create_sqlite_engine", opened)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 2
    assert "KNOWLEDGE_API_KEY" in result.output
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_default_database_without_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    database = tmp_path / ".konwledge" / "knowledge.db"
    database.parent.mkdir()
    database.touch()
    called = Mock()

    def run(application: Any, **kwargs: Any) -> None:
        called()
        with TestClient(application) as client:
            assert client.get("/v1/models").status_code == 200

    monkeypatch.setattr(uvicorn, "run", run)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    called.assert_called_once()


@pytest.mark.parametrize("stage", ["schema", "app", "run", "system_exit"])
def test_runtime_errors_safe_and_dispose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    secret = "private-key-do-not-print"
    monkeypatch.setenv("KNOWLEDGE_API_KEY", secret)
    engine = create_sqlite_engine(f"sqlite:///{database}")
    disposed = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", disposed)
    monkeypatch.setattr(server, "create_sqlite_engine", lambda url: engine)
    failure = Mock(side_effect=SystemExit(1) if stage == "system_exit" else RuntimeError(secret))
    if stage == "schema":
        monkeypatch.setattr(server, "create_schema", failure)
    elif stage == "app":
        monkeypatch.setattr(server, "create_app", failure)
    else:
        monkeypatch.setattr(uvicorn, "run", failure)
    result = runner.invoke(app, ["serve", "-d", str(database)])
    assert result.exit_code == 1, result.output
    assert secret not in result.output
    assert "Traceback" not in result.output
    disposed.assert_called_once()
    assert list(tmp_path.iterdir()) == [database]


@pytest.mark.parametrize("dependency", ["fastapi", "uvicorn"])
def test_missing_extra_friendly_before_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency: str
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    opened = Mock()
    monkeypatch.setattr(server, "create_sqlite_engine", opened)
    original_import = builtins.__import__

    def block_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == dependency:
            raise ImportError("private exception details")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_import)
    result = runner.invoke(app, ["serve", "-d", str(database)])
    assert result.exit_code == 1
    assert "konwledge[api]" in result.output
    assert "private exception details" not in result.output
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == [database]


@pytest.mark.parametrize("arguments", [["--version"], ["--help"], ["serve", "--help"]])
def test_help_version_without_extra_or_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert list(tmp_path.iterdir()) == []


def test_serve_does_not_accept_key_value_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["serve", "--api-key", "secret-value"])
    assert result.exit_code == 2
    assert "secret-value" not in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ["engine", "dispose"])
def test_engine_creation_or_disposal_failure_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    database = tmp_path / "existing.db"
    database.touch()
    secret = "private-engine-secret"
    monkeypatch.setenv("KNOWLEDGE_API_KEY", secret)
    if stage == "engine":
        monkeypatch.setattr(server, "create_sqlite_engine", Mock(side_effect=RuntimeError(secret)))
    else:
        engine = create_sqlite_engine(f"sqlite:///{database}")
        original_dispose = engine.dispose

        def dispose() -> None:
            original_dispose()
            raise ValueError(secret)

        monkeypatch.setattr(engine, "dispose", dispose)
        monkeypatch.setattr(server, "create_sqlite_engine", lambda url: engine)
        monkeypatch.setattr(uvicorn, "run", Mock())
    result = runner.invoke(app, ["serve", "--database", str(database)])
    assert result.exit_code == 1, result.output
    assert secret not in result.output
    assert "Traceback" not in result.output
    assert list(tmp_path.iterdir()) == [database]


def test_database_disappears_after_validation_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "vanishing.db"
    database.touch()
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    called = Mock()

    def open_after_delete(url: str) -> Engine:
        database.unlink()
        return create_sqlite_engine(url)

    monkeypatch.setattr(server, "create_sqlite_engine", open_after_delete)
    monkeypatch.setattr(uvicorn, "run", called)
    result = runner.invoke(app, ["serve", "--database", str(database)])
    assert result.exit_code == 1
    assert list(tmp_path.iterdir()) == []
    called.assert_not_called()
