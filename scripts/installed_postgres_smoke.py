"""隔离安装 [postgres] 的验证；仅使用合成 DSN，绝不连接业务数据库。"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import site
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _is_installed(path: Path) -> bool:
    resolved = path.resolve()
    prefix = Path(sys.prefix).resolve()
    return resolved.is_relative_to(prefix) and any(
        resolved.is_relative_to(Path(directory).resolve()) for directory in site.getsitepackages()
    )


def verify_installed(expected_version: str) -> tuple[str, str]:
    psycopg = importlib.import_module("psycopg")
    psycopg_pq = importlib.import_module("psycopg.pq")

    import konwledge

    require(konwledge.__version__ == expected_version, "installed Konwledge version mismatch")
    require(
        importlib.metadata.version("konwledge") == expected_version,
        "installed metadata version mismatch",
    )
    psycopg_version = importlib.metadata.version("psycopg")
    version_parts = psycopg_version.split(".")
    try:
        major_minor = tuple(int(part) for part in version_parts[:2])
    except ValueError as error:
        raise AssertionError("psycopg version is not numeric") from error
    require((3, 2) <= major_minor < (4, 0), "psycopg version is outside >=3.2,<4")
    implementation: str = psycopg_pq.__impl__
    require(implementation == "binary", "psycopg is not using its binary implementation")

    for module in (konwledge, psycopg):
        filename = module.__file__
        if filename is None:
            raise AssertionError(f"installed module has no file: {module.__name__}")
        require(
            _is_installed(Path(filename)),
            f"module was not imported from this venv: {module.__name__}",
        )
    for package in ("fastapi", "uvicorn", "lancedb", "pyarrow"):
        require(
            importlib.util.find_spec(package) is None,
            f"postgres-only install leaked unrelated optional dependency: {package}",
        )
    return psycopg_version, implementation


def verify_collector_without_connection() -> None:
    from konwledge.database_schema import (
        DatabaseConnectionError,
        PostgreSQLSchemaCollector,
        SchemaCollector,
    )

    require(
        issubclass(PostgreSQLSchemaCollector, SchemaCollector),
        "PostgreSQL database schema collector is unavailable",
    )
    secret = "synthetic-postgres-smoke-password"
    dsn = (
        f"postgresql://synthetic_user:{secret}@localhost:1/synthetic_database"
        f"?application_name={secret}"
    )
    collector = PostgreSQLSchemaCollector(dsn)
    observed: dict[str, Any] = {}

    class SyntheticEngine:
        disposed = False

        def connect(self) -> Any:
            observed["connect_called"] = True
            raise SQLAlchemyError(f"synthetic connection failure containing {secret}")

        def dispose(self) -> None:
            self.disposed = True

    engine = SyntheticEngine()

    def create_synthetic_engine(url: Any, **kwargs: Any) -> SyntheticEngine:
        observed.update(
            {
                "drivername": url.drivername,
                "host": url.host,
                "port": url.port,
                "database": url.database,
                "query": dict(url.query),
                "kwargs": kwargs,
            }
        )
        return engine

    with patch("konwledge.database_schema.collector.create_engine", create_synthetic_engine):
        try:
            collector.capture()
        except DatabaseConnectionError as error:
            message = str(error)
        else:
            raise AssertionError("synthetic connection failure was not sanitized")

    require(observed.get("connect_called") is True, "synthetic failure path was not exercised")
    require(observed.get("drivername") == "postgresql+psycopg", "standard DSN was not normalized")
    require(observed.get("host") == "localhost", "synthetic DSN host changed")
    require(observed.get("port") == 1, "synthetic DSN port changed")
    require(observed.get("database") == "synthetic_database", "synthetic DSN database changed")
    require(observed.get("query") == {}, "DSN query parameters were retained")
    require(engine.disposed, "synthetic engine was not disposed")
    require(message == "PostgreSQL schema capture failed", "connection failure was not sanitized")
    require(
        secret not in message and "synthetic_user" not in message,
        "synthetic credential leaked",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    try:
        psycopg_version, implementation = verify_installed(args.expected_version)
        verify_collector_without_connection()
    except (AssertionError, ImportError, OSError, SQLAlchemyError, ValueError) as error:
        print(f"installed postgres smoke failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "version": args.expected_version,
                "checks": [
                    "installed_postgres_extra",
                    "psycopg_binary",
                    "database_schema_collector",
                    "standard_dsn_normalization",
                    "sanitized_synthetic_failure",
                    "no_unrelated_extras",
                ],
                "psycopg": {
                    "version": psycopg_version,
                    "implementation": implementation,
                },
                "business_database_connected": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
