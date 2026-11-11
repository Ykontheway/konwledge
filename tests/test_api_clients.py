"""官方 SDK 的真实序列化/解析验收；HTTPX 仅把请求转发给本地 ASGI。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI
from openai import BadRequestError, OpenAI
from pydantic import SecretStr

from konwledge.api import create_app
from konwledge.application import KnowledgeLibrary
from konwledge.context import ContextPacket
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

KEY = "local-sdk-test-key"
BASE_URL = "http://127.0.0.1:8000/v1"
QUERY = 'Sdkaccept "引号" evidence'


@pytest.fixture
def acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, httpx.Client, dict[str, Any]]]:
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    engine = create_sqlite_engine("sqlite:///:memory:")
    create_schema(engine)
    library = KnowledgeLibrary(
        create_session_factory(engine), clock=lambda: datetime(2026, 11, 1, tzinfo=UTC)
    )
    source = tmp_path / "sdk.md"
    source.write_text('Sdkaccept "引号" evidence.\n保留 Unicode 与真实请求正文。', encoding="utf-8")
    imported = library.import_path(source)
    source_id = imported.imported[0].result.ingestion.document.source_id
    controls: dict[str, Any] = {
        "source_ids": [str(source_id)],
        "as_of": "2026-11-11T12:00:00Z",
        "mode": "lexical",
        "top_k": 3,
        "candidate_limit": 10,
        "rrf_k": 40,
        "max_chars": 6000,
        "max_units": 2,
        "include_stale": False,
    }
    try:
        with TestClient(create_app(library, api_key=KEY)) as asgi:

            def dispatch(request: httpx.Request) -> httpx.Response:
                # 不重写 SDK payload、不移除不支持字段；任何其他目标即刻失败。
                assert request.url.host == "127.0.0.1"
                assert request.url.path == "/v1/chat/completions"
                assert request.method == "POST"
                assert request.headers["authorization"] == f"Bearer {KEY}"
                payload = json.loads(request.content)
                assert payload["messages"] == [{"role": "user", "content": QUERY}]
                assert payload["context"] == controls
                response = asgi.request(
                    request.method,
                    request.url.path
                    + ("?" + request.url.query.decode() if request.url.query else ""),
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=response.content,
                    request=request,
                )

            with httpx.Client(transport=httpx.MockTransport(dispatch)) as http_client:
                yield asgi, http_client, controls
    finally:
        engine.dispose()


def native_packet(asgi: TestClient, controls: dict[str, Any]) -> ContextPacket:
    response = asgi.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"query": QUERY, **controls},
    )
    assert response.status_code == 200
    packet = ContextPacket.model_validate_json(response.content)
    assert len(packet.blocks) == 1
    assert "Unicode" in packet.blocks[0].citation.quote
    return packet


def test_official_openai_sdk_equals_native(
    acceptance: tuple[TestClient, httpx.Client, dict[str, Any]],
) -> None:
    asgi, http_client, controls = acceptance
    expected = native_packet(asgi, controls)
    with OpenAI(api_key=KEY, base_url=BASE_URL, http_client=http_client, max_retries=0) as sdk:
        completion = sdk.chat.completions.create(
            model="knowledge-context",
            messages=[{"role": "user", "content": QUERY}],
            stream=False,
            extra_body={"context": controls},
        )
    content = completion.choices[0].message.content
    assert content is not None
    assert ContextPacket.model_validate_json(content) == expected
    assert completion.choices[0].message.role == "assistant"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage is None
    assert "usage" not in completion.model_fields_set


def test_langchain_chatopenai_equals_native(
    acceptance: tuple[TestClient, httpx.Client, dict[str, Any]],
) -> None:
    asgi, http_client, controls = acceptance
    expected = native_packet(asgi, controls)
    chat = ChatOpenAI(
        model="knowledge-context",
        api_key=SecretStr(KEY),
        base_url=BASE_URL,
        http_client=http_client,
        temperature=None,
        streaming=False,
        max_retries=0,
        use_responses_api=False,
        extra_body={"context": controls},
    )
    message = chat.invoke(QUERY)
    assert isinstance(message.content, str)
    assert ContextPacket.model_validate_json(message.content) == expected
    assert message.usage_metadata is None
    assert message.response_metadata.get("token_usage") in (None, {})


@pytest.mark.parametrize(
    "unsupported", [{"temperature": 0}, {"tools": []}, {"stream": True}, {"top_p": 1}]
)
def test_sdk_unsupported_fields_are_not_erased(
    acceptance: tuple[TestClient, httpx.Client, dict[str, Any]], unsupported: dict[str, Any]
) -> None:
    _, http_client, controls = acceptance
    with (
        OpenAI(api_key=KEY, base_url=BASE_URL, http_client=http_client, max_retries=0) as sdk,
        pytest.raises(BadRequestError) as error,
    ):
        sdk.chat.completions.create(
            model="knowledge-context",
            messages=[{"role": "user", "content": QUERY}],
            extra_body={"context": controls, **unsupported},
        )
    assert error.value.status_code == 400
    assert KEY not in str(error.value)
