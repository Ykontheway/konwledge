"""本机 API 启动入口；可选依赖只在实际启动时导入。"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import URL

from konwledge.api.app import create_app
from konwledge.application import KnowledgeLibrary
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine


class ServerRuntimeError(RuntimeError):
    """不携带底层异常内容的启动/运行错误。"""


def run_server(
    database: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    enable_management: bool = False,
) -> None:
    """校验本地配置，打开已有数据库，阻塞运行，并始终释放 engine。

    启动允许 additive schema/FTS 初始化，HTTP 请求只读。SQLite mode=rw
    也防止校验后文件被移除时意外创建空库。localhost 映射到 IPv4 loopback，
    不依赖 DNS；密钥只读取环境变量，不作为 CLI 参数接受或输出。
    """

    if not isinstance(enable_management, bool):
        raise TypeError("enable_management must be a bool")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("--host 仅允许 127.0.0.1、::1 或 localhost")
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("--port 必须在 1..65535 之间")
    api_key = os.environ.get("KNOWLEDGE_API_KEY")
    if api_key is not None and not api_key.strip():
        raise ValueError("显式设置的 KNOWLEDGE_API_KEY 不得为空或仅含空白")
    if enable_management and api_key is None:
        raise ValueError("--enable-management 需要通过环境变量 KNOWLEDGE_API_KEY 设置访问密钥")
    database_path = database.expanduser().resolve()
    if not database_path.is_file():
        raise ValueError("--database 必须指向已有数据库文件；请先使用 knowledge import")

    # 缺少 extra 必须在打开 SQLite 前失败；其他 CLI 命令不导入 HTTP 依赖。
    import uvicorn
    from fastapi import FastAPI  # noqa: F401

    engine = None
    try:
        uri_path = quote(database_path.as_posix(), safe="/")
        url = URL.create(
            "sqlite+pysqlite",
            database=f"file:{uri_path}",
            query={"mode": "rw", "uri": "true"},
        )
        engine = create_sqlite_engine(url.render_as_string(hide_password=False))
        # Management readers inspect engine.url.database to open a separate ro handle;
        # keep the underlying mode=rw creator while exposing the filesystem path.
        engine.url = engine.url.set(database=database_path.as_posix())
        create_schema(engine)
        library = KnowledgeLibrary(create_session_factory(engine))
        application = create_app(library, api_key=api_key, enable_management=enable_management)
        uvicorn.run(
            application,
            host="127.0.0.1" if host == "localhost" else host,
            port=port,
            access_log=False,
        )
    except (Exception, SystemExit):
        # 原始异常可能包含密钥、SQL、路径或请求内容，不能反射到 CLI。
        raise ServerRuntimeError("本地 API 启动或运行失败；请检查数据库与端口配置") from None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                raise ServerRuntimeError("本地 API engine 释放失败") from None
