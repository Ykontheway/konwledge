"""本机 B1 + Vite 联调；临时凭据仅交给自己的子进程，不创建存储。"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, suppress
from pathlib import Path
from types import FrameType

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchError(Exception):
    """仅承载固定安全说明，不附带底层 exception/env/header。"""


def port_number(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须为 1..65535 的整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须为 1..65535 的整数")
    return port


def preflight(database: Path, web_port: int, api_port: int) -> Path:
    if (
        isinstance(web_port, bool)
        or isinstance(api_port, bool)
        or not 1 <= web_port <= 65535
        or not 1 <= api_port <= 65535
        or web_port == api_port
    ):
        raise WorkbenchError("端口必须有效且互不相同。")
    try:
        path = database.expanduser().resolve(strict=True)
        if not path.is_file():
            raise WorkbenchError("必须选择已有 SQLite 数据库文件。")
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise WorkbenchError("必须选择已有 SQLite 数据库文件。")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise WorkbenchError("已有数据库校验未通过。")
        finally:
            connection.close()
        with ExitStack() as stack:
            for port in (web_port, api_port):
                sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                sock.bind(("127.0.0.1", port))
    except (OSError, sqlite3.Error):
        raise WorkbenchError("预检失败：数据库不可读或所选 loopback 端口已被占用。") from None
    return path


def child_environments(
    parent: Mapping[str, str], api_port: int, secret: str
) -> tuple[dict[str, str], dict[str, str]]:
    # 不继承 VITE_*、已有业务密钥、NODE_OPTIONS 或完整父进程环境。
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "TMPDIR",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "UV_PYTHON",
        "PNPM_HOME",
        "SYSTEMROOT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
    }
    base = {key: value for key, value in parent.items() if key in allowed or key.startswith("LC_")}
    # 即便错误地把本次 secret 放入可继承变量，也不能再通过其它变量传播。
    base = {key: value for key, value in base.items() if secret not in value}
    backend = {**base, "KNOWLEDGE_API_KEY": secret}
    web = {
        **backend,
        "KNOWLEDGE_MANAGEMENT_ORIGIN": f"http://127.0.0.1:{api_port}",
    }
    return backend, web


def spawn(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    # 不转发子进程日志：避免第三方启动失败时把 env/header/堆栈写到终端。
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_backend(
    process: subprocess.Popen[bytes], port: int, stop: threading.Event, timeout: float = 30
) -> None:
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise WorkbenchError("只读 API 启动失败；请检查依赖和已有数据库。")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            payload = response.read(2049)
            if response.status == 200 and len(payload) <= 2048:
                health = json.loads(payload)
                if isinstance(health, dict) and health.get("status") == "ok":
                    return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            connection.close()
        stop.wait(0.1)
    if not stop.is_set():
        raise WorkbenchError("等待只读 API 就绪超时；未启动网页服务。")


def wait_web(
    children: Sequence[subprocess.Popen[bytes]],
    port: int,
    stop: threading.Event,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise WorkbenchError("联调子进程启动失败。")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            stop.wait(0.1)
    if not stop.is_set():
        raise WorkbenchError("等待网页服务就绪超时。")


def cleanup(children: Sequence[subprocess.Popen[bytes]]) -> None:
    # 每个 Popen 创建独立 session/group；只关闭由本启动器创建的组，包括 uv/pnpm 后代。
    # 即使组长已退出，仍清理该组；绝不按端口/pkill/全机进程名杀进程。
    for child in reversed(children):
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
    for child in reversed(children):
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=5)
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def run(database: Path, web_port: int, api_port: int) -> int:
    children: list[subprocess.Popen[bytes]] = []
    stop = threading.Event()
    received_signal = 0
    previous = {}

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal received_signal
        received_signal = signum
        stop.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        path = preflight(database, web_port, api_port)
        if stop.is_set():
            return 128 + received_signal
        secret = secrets.token_urlsafe(32)
        backend_env, web_env = child_environments(os.environ, api_port, secret)
        backend = spawn(
            [
                "uv",
                "run",
                "knowledge",
                "serve",
                "--enable-management",
                "--database",
                str(path),
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            ROOT,
            backend_env,
        )
        children.append(backend)
        wait_backend(backend, api_port, stop)
        if stop.is_set():
            return 128 + received_signal
        web = spawn(
            [
                "pnpm",
                "run",
                "dev",
                "--host",
                "127.0.0.1",
                "--port",
                str(web_port),
                "--strictPort",
            ],
            ROOT / "web",
            web_env,
        )
        children.append(web)
        wait_web(children, web_port, stop)
        if not stop.is_set():
            print(
                f"联调已就绪：http://127.0.0.1:{web_port}/ （Ctrl+C 关闭自己的子进程）",
                flush=True,
            )
        while not stop.wait(0.2):
            if any(child.poll() is not None for child in children):
                raise WorkbenchError("联调子进程已退出；正在关闭其它联调子进程。")
        return 128 + received_signal
    except WorkbenchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("联调启动失败；请检查本机 uv/pnpm/依赖和端口。", file=sys.stderr)
        return 1
    except Exception:
        print("联调发生异常；正在安全关闭自己的子进程。", file=sys.stderr)
        return 1
    finally:
        try:
            cleanup(children)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--web-port", type=port_number, default=5175)
    parser.add_argument("--api-port", type=port_number, default=8015)
    args = parser.parse_args(argv)
    return run(args.database, args.web_port, args.api_port)


if __name__ == "__main__":
    sys.exit(main())
