"""验证独立 [tokens] 安装中的真实 tiktoken cl100k_base 与上下文 token 预算。

本脚本不使用 mock，不调用模型或 SDK。tiktoken 首次取得公开 encoding 表时可能访问网络；
编排器通过标准 TIKTOKEN_CACHE_DIR 在隔离环境间共享该公开表的本地缓存。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import site
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def is_installed(path: Path) -> bool:
    resolved = path.resolve()
    prefix = Path(sys.prefix).resolve()
    return resolved.is_relative_to(prefix) and any(
        resolved.is_relative_to(Path(directory).resolve()) for directory in site.getsitepackages()
    )


def numeric_version(value: str) -> tuple[int, ...]:
    pieces: list[int] = []
    for piece in value.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        pieces.append(int(digits))
    if not pieces:
        raise AssertionError(f"non-numeric tiktoken version: {value!r}")
    return tuple(pieces)


def verify_identity(expected_version: str) -> tuple[Any, str]:
    import tiktoken

    import konwledge

    require(konwledge.__version__ == expected_version, "installed Konwledge version mismatch")
    require(
        importlib.metadata.version("konwledge") == expected_version, "metadata version mismatch"
    )
    tiktoken_version = importlib.metadata.version("tiktoken")
    parsed = numeric_version(tiktoken_version)
    require(parsed >= (0, 12) and parsed < (1,), "tiktoken version is outside >=0.12,<1")
    for module in (konwledge, tiktoken):
        filename = module.__file__
        require(
            filename is not None and is_installed(Path(filename)),
            f"not installed: {module.__name__}",
        )
    for dependency in ("fastapi", "uvicorn", "psycopg", "lancedb", "pyarrow"):
        require(
            importlib.util.find_spec(dependency) is None,
            f"tokens-only install leaked unrelated optional dependency: {dependency}",
        )
    return tiktoken, tiktoken_version


def verify_real_encoding(tiktoken: Any) -> tuple[Any, dict[str, int]]:
    from konwledge.context import TiktokenTokenCounter

    get_encoding = tiktoken.get_encoding
    encoding = get_encoding("cl100k_base")
    counter = TiktokenTokenCounter()
    samples = {
        "english": "The installed token counter is exact.",
        "chinese": "真实中文分词必须在隔离安装中运行。",
        "emoji": "release ready 🚀✅",
        "special_spelling": "ordinary evidence containing <|endoftext|> literally",
        "ordinary": "alpha beta gamma 123",
    }
    counts: dict[str, int] = {}
    for name, text in samples.items():
        expected = len(encoding.encode(text, disallowed_special=()))
        actual = counter.count(text)
        require(actual == expected and actual > 0, f"real cl100k_base mismatch for {name}")
        counts[name] = actual
    require(
        encoding.decode(encoding.encode(samples["special_spelling"], disallowed_special=()))
        == samples["special_spelling"],
        "special-token spelling was not treated as ordinary text",
    )
    return encoding, counts


def verify_context_budget(encoding: Any) -> dict[str, int]:
    from konwledge import KnowledgeLibrary
    from konwledge.context import TokenContextPacket
    from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

    with tempfile.TemporaryDirectory(prefix="konwledge-tokens-installed-") as temporary:
        root = Path(temporary)
        source = root / "token-evidence.md"
        evidence = "Tokenbudget exact evidence 中文 🚀 and <|endoftext|> stays ordinary."
        source.write_text(evidence, encoding="utf-8")
        engine = create_sqlite_engine(f"sqlite+pysqlite:///{(root / 'tokens.db').as_posix()}")
        create_schema(engine)
        library = KnowledgeLibrary(create_session_factory(engine))
        try:
            library.import_path(source)
            generous = cast(
                TokenContextPacket,
                library.build_context(
                    "Tokenbudget",
                    max_chars=10_000,
                    max_units=1,
                    max_tokens=10_000,
                ),
            )
            require(
                isinstance(generous, TokenContextPacket), "token budget did not return token packet"
            )
            require(len(generous.blocks) == 1, "generous token budget omitted real evidence")
            exact = len(encoding.encode(generous.context_text, disallowed_special=()))
            require(generous.used_tokens == exact, "used_tokens is not the real cl100k_base count")
            require(generous.token_encoding == "cl100k_base", "encoding name mismatch")

            fitting = cast(
                TokenContextPacket,
                library.build_context(
                    "Tokenbudget",
                    max_chars=10_000,
                    max_units=1,
                    max_tokens=exact,
                ),
            )
            require(
                len(fitting.blocks) == 1 and fitting.used_tokens == exact, "exact budget rejected"
            )
            constrained = cast(
                TokenContextPacket,
                library.build_context(
                    "Tokenbudget",
                    max_chars=10_000,
                    max_units=1,
                    max_tokens=exact - 1,
                ),
            )
            require(not constrained.blocks, "over-budget evidence was included")
            require(constrained.used_tokens == 0, "empty context should use zero tokens")
            require(
                bool(constrained.skipped) and constrained.skipped[0].reason.value == "budget",
                "token-budget skip reason is missing",
            )
        finally:
            engine.dispose()
    return {"exact_budget": exact, "constrained_budget": exact - 1}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    try:
        tiktoken, version = verify_identity(args.expected_version)
        encoding, counts = verify_real_encoding(tiktoken)
        budget = verify_context_budget(encoding)
    except (AssertionError, ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"installed tokens smoke failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "version": args.expected_version,
                "tiktoken_version": version,
                "encoding": "cl100k_base",
                "real_sample_counts": counts,
                "context_budget": budget,
                "checks": [
                    "installed_site_packages_identity",
                    "tiktoken_version_range",
                    "real_english_chinese_emoji_encoding",
                    "special_token_spelling_as_ordinary_text",
                    "exact_context_max_tokens_budget",
                    "no_unrelated_extras",
                ],
                "mock_used": False,
                "model_or_sdk_requests": False,
                "network_scope": "only tiktoken public encoding table on first uncached load",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
