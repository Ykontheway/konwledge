"""隔离安装 [api] 的 ASGI 验证；不启动监听、不安装 SDK、不访问外部网络。"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import site
import sys
import tempfile
from collections.abc import MutableMapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from fastapi import FastAPI
from sqlalchemy import Engine, event

from konwledge import KnowledgeLibrary, __version__
from konwledge.api import app as app_module
from konwledge.api import server
from konwledge.api.app import create_app
from konwledge.cli import main as cli_main
from konwledge.context import ContextPacket
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

KEY = "isolated-api-smoke-key"


async def request(
    application: FastAPI,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    auth: bool = True,
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode() if body is not None else b""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")]
        + ([(b"authorization", f"Bearer {KEY}".encode())] if auth else []),
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(dict(message))

    await application(scope, receive, send)
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    content = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(status), json.loads(content)


def verify_routes(application: FastAPI) -> None:
    status, health = asyncio.run(request(application, "GET", "/health", auth=False))
    assert status == 200 and health["version"] == __version__
    status, _ = asyncio.run(request(application, "GET", "/v1/models", auth=False))
    assert status == 401
    status, models = asyncio.run(request(application, "GET", "/v1/models"))
    assert status == 200 and models["data"][0]["id"] == "knowledge-context"
    controls = {"as_of": "2026-11-11T12:00:00Z", "top_k": 3, "max_units": 2}
    status, native = asyncio.run(
        request(application, "POST", "/v1/context", {"query": "Isolatedapi", **controls})
    )
    assert status == 200
    packet = ContextPacket.model_validate_json(json.dumps(native))
    assert len(packet.blocks) == 1 and "精确证据" in packet.blocks[0].citation.quote
    status, chat = asyncio.run(
        request(
            application,
            "POST",
            "/v1/chat/completions",
            {
                "model": "knowledge-context",
                "messages": [{"role": "user", "content": "Isolatedapi"}],
                "context": controls,
                "stream": False,
            },
        )
    )
    assert status == 200 and "usage" not in chat
    assert ContextPacket.model_validate_json(chat["choices"][0]["message"]["content"]) == packet
    status, error = asyncio.run(
        request(application, "POST", "/v1/context", {"query": "q", "temperature": 0})
    )
    assert status == 400 and error["error"]["code"] == "unsupported_option"


def verify_installed() -> None:
    for module in (app_module, server):
        filename = module.__file__
        assert filename is not None
        path = Path(filename).resolve()
        assert path.is_relative_to(Path(sys.prefix).resolve())
        assert any(
            path.is_relative_to(Path(directory).resolve()) for directory in site.getsitepackages()
        )
    for package in ("openai", "langchain_openai", "httpx"):
        assert importlib.util.find_spec(package) is None, f"SDK/test dependency leaked: {package}"


def verify(root: Path) -> None:
    source = root / "evidence.md"
    source.write_text("Isolatedapi 精确证据。", encoding="utf-8")
    database = root / "library.db"
    engine = create_sqlite_engine(f"sqlite:///{database}")
    create_schema(engine)
    library = KnowledgeLibrary(
        create_session_factory(engine), clock=lambda: datetime(2026, 11, 1, tzinfo=UTC)
    )
    writes: list[str] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().split(maxsplit=1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "DROP",
            "ALTER",
        }:
            writes.append(statement)

    try:
        library.import_path(source)
        event.listen(engine, "before_cursor_execute", capture_sql)
        verify_routes(create_app(library, api_key=KEY))
        assert writes == [], "HTTP request performed a write"
    finally:
        engine.dispose()

    disposed = Mock()
    called = Mock()

    def capture_engine(url: str) -> Engine:
        opened = create_sqlite_engine(url)
        original = opened.dispose

        def dispose(close: bool = True) -> None:
            disposed()
            original(close=close)

        opened.dispose = dispose  # type: ignore[method-assign]
        return opened

    def run(application: FastAPI, **kwargs: Any) -> None:
        assert kwargs == {"host": "127.0.0.1", "port": 8000, "access_log": False}
        verify_routes(application)
        called()

    with (
        patch.dict("os.environ", {"KNOWLEDGE_API_KEY": KEY}),
        patch.object(server, "create_sqlite_engine", capture_engine),
        patch("uvicorn.run", run),
    ):
        assert cli_main(["serve", "--database", str(database), "--host", "localhost"]) == 0
    called.assert_called_once()
    disposed.assert_called_once()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    assert __version__ == args.expected_version
    verify_installed()
    with tempfile.TemporaryDirectory(prefix="konwledge-api-installed-") as temporary:
        verify(Path(temporary))
    print(
        json.dumps(
            {
                "version": __version__,
                "checks": [
                    "installed_api",
                    "no_sdks",
                    "asgi_routes",
                    "readonly",
                    "cli_startup_disposal",
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
