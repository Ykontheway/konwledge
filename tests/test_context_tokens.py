from __future__ import annotations

import importlib
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from konwledge.context import (
    ContextPacket,
    ContextService,
    ContextSkipReason,
    TiktokenTokenCounter,
    TokenContextPacket,
    assemble_context,
)
from test_context import make_hit, make_result, make_unit


class Utf8TokenCounter:
    """Deterministic injectable counter that is deliberately not a character count."""

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


def test_default_packet_type_and_json_shape_are_unchanged() -> None:
    packet = assemble_context(make_result(hits=(make_hit(),)))

    assert type(packet) is ContextPacket
    assert "max_tokens" not in packet.model_dump()
    assert "used_tokens" not in packet.model_dump()
    assert "token_encoding" not in packet.model_dump()


def test_token_budget_selects_whole_blocks_and_recalculates_final_count() -> None:
    short = make_unit(
        id=uuid4(),
        lineage_id=uuid4(),
        start_char=10_000,
        text="中文证据 🧪 <|endoftext|>",
    )
    long = make_unit(
        id=uuid4(),
        lineage_id=uuid4(),
        start_char=0,
        text="很长的证据🙂" * 200,
    )
    counter = Utf8TokenCounter()
    short_only = assemble_context(
        make_result(hits=(make_hit(unit=short),)),
        max_tokens=100_000,
        token_counter=counter,
    )

    assert isinstance(short_only, TokenContextPacket)
    packet = assemble_context(
        make_result(hits=(make_hit(unit=long), make_hit(unit=short))),
        max_tokens=short_only.used_tokens,
        token_counter=counter,
    )

    assert isinstance(packet, TokenContextPacket)
    assert [block.citation.knowledge_unit_id for block in packet.blocks] == [short.id]
    assert packet.blocks[0].label == "[1]"
    assert packet.skipped[0].unit_id == long.id
    assert packet.skipped[0].reason is ContextSkipReason.BUDGET
    assert packet.blocks[0].citation.quote == short.text
    assert packet.used_tokens == counter.count(packet.context_text)
    assert packet.used_tokens == packet.max_tokens


def test_zero_token_budget_returns_token_packet_with_no_partial_evidence() -> None:
    packet = assemble_context(
        make_result(hits=(make_hit(),)),
        max_tokens=0,
        token_counter=Utf8TokenCounter(),
    )

    assert isinstance(packet, TokenContextPacket)
    assert packet.blocks == ()
    assert packet.context_text == ""
    assert packet.used_tokens == 0
    assert [item.reason for item in packet.skipped] == [ContextSkipReason.BUDGET]


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, "10"])
def test_max_tokens_is_strictly_validated(invalid: object) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        assemble_context(
            make_result(),
            max_tokens=cast(Any, invalid),
            token_counter=Utf8TokenCounter(),
        )


@pytest.mark.parametrize("invalid", ["", "   ", 123, None])
def test_token_encoding_is_validated_when_budget_is_enabled(invalid: object) -> None:
    with pytest.raises(ValueError, match="token_encoding"):
        assemble_context(
            make_result(),
            max_tokens=10,
            token_encoding=cast(Any, invalid),
            token_counter=Utf8TokenCounter(),
        )


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, "2", None])
def test_counter_result_must_be_a_non_negative_non_bool_integer(invalid: object) -> None:
    counter = MagicMock()
    counter.count.return_value = invalid

    with pytest.raises(ValueError, match=r"token_counter\.count"):
        assemble_context(make_result(), max_tokens=10, token_counter=counter)


def test_counter_must_expose_callable_count() -> None:
    with pytest.raises(ValueError, match="callable count"):
        assemble_context(make_result(), max_tokens=10, token_counter=cast(Any, object()))


def test_token_packet_validates_budget_and_round_trips() -> None:
    packet = assemble_context(
        make_result(hits=(make_hit(),)),
        max_tokens=10_000,
        token_encoding="custom-exact-v1",
        token_counter=Utf8TokenCounter(),
    )
    restored = TokenContextPacket.model_validate_json(packet.model_dump_json())

    assert restored == packet
    assert packet.token_encoding == "custom-exact-v1"
    with pytest.raises(ValidationError, match="used_tokens exceeds max_tokens"):
        TokenContextPacket.model_validate(
            {**packet.model_dump(), "max_tokens": packet.used_tokens - 1}
        )


class _FakeEncoding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def encode(self, text: str, *, disallowed_special: tuple[object, ...]) -> list[int]:
        self.calls.append((text, disallowed_special))
        return list(text.encode("utf-8"))


def test_tiktoken_is_lazy_and_special_tokens_are_ordinary_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _FakeEncoding()
    fake_tiktoken = MagicMock()
    fake_tiktoken.get_encoding.return_value = encoding
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> object:
        if name == "tiktoken":
            return fake_tiktoken
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    counter = TiktokenTokenCounter("cl100k_base")
    evidence = "中文 🧪 <|endoftext|>"

    assert counter.count(evidence) == len(evidence.encode("utf-8"))
    assert encoding.calls == [(evidence, ())]
    fake_tiktoken.get_encoding.assert_called_once_with("cl100k_base")


def test_default_counter_reports_missing_dependency_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_tiktoken(name: str) -> object:
        assert name == "tiktoken"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(importlib, "import_module", missing_tiktoken)
    with pytest.raises(ImportError, match=r"konwledge\[tokens\]"):
        assemble_context(make_result(), max_tokens=10)


def test_default_counter_reports_unknown_encoding_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tiktoken = MagicMock()
    fake_tiktoken.get_encoding.side_effect = ValueError("unknown encoding")
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_tiktoken)

    with pytest.raises(ValueError, match=r"unknown tiktoken encoding.*not-real"):
        assemble_context(make_result(), max_tokens=10, token_encoding="not-real")


def test_context_service_assemble_forwards_token_options() -> None:
    retrieval = MagicMock()
    service = ContextService(retrieval)

    packet = service.assemble(
        make_result(hits=(make_hit(),)),
        max_tokens=10_000,
        token_encoding="custom",
        token_counter=Utf8TokenCounter(),
    )

    assert isinstance(packet, TokenContextPacket)
    assert packet.token_encoding == "custom"
    retrieval.search.assert_not_called()


def test_context_service_build_preloads_default_counter_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = MagicMock()
    service = ContextService(retrieval)

    def missing_tiktoken(name: str) -> object:
        assert name == "tiktoken"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(importlib, "import_module", missing_tiktoken)
    with pytest.raises(ImportError, match="tiktoken"):
        service.build("query", max_tokens=100)

    retrieval.search.assert_not_called()


def test_context_service_build_preloads_encoding_and_reuses_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _FakeEncoding()
    fake_tiktoken = MagicMock()
    fake_tiktoken.get_encoding.return_value = encoding
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_tiktoken)
    retrieval = MagicMock()
    retrieval.search.return_value = make_result(hits=(make_hit(),))
    service = ContextService(retrieval)

    packet = service.build("query", max_tokens=10_000, token_encoding="cl100k_base")

    assert isinstance(packet, TokenContextPacket)
    assert fake_tiktoken.get_encoding.call_count == 1
    retrieval.search.assert_called_once()
