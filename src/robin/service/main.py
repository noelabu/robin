"""Running the service: `robin serve` / `robin`."""

import asyncio
import json
import socket
from pathlib import Path

import uvicorn

from robin.paths import secure_touch
from robin.service.app import create_app
from robin.service.auth import Auth
from robin.service.runtime import ServiceRuntime
from robin.store import Store, open_store

DEFAULT_PORT = 7333


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_endpoint(store: Store, port: int) -> None:
    """Where the CLI finds the running service."""
    path = secure_touch(store.data_dir / "service.json")
    path.write_text(json.dumps({"port": port, "url": f"http://127.0.0.1:{port}"}))


def build(store: Store, port: int, *, start_supervisor: bool = True) -> tuple[uvicorn.Server, Auth]:
    auth = Auth.for_data_dir(store.data_dir, port)
    runtime = ServiceRuntime(store, start_supervisor=start_supervisor)
    app = create_app(runtime, auth)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    return uvicorn.Server(config), auth


def serve(data_dir: Path | None = None, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    store = open_store(data_dir)  # one service per data directory (store lock)
    try:
        server, auth = build(store, port)
        write_endpoint(store, port)
        url = f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}"
        print(
            f"Robin is running. Open: {url}\n(the link works once and expires in 60 seconds; "
            "`robin open` makes a new one)",
            flush=True,
        )
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        asyncio.run(server.serve())
    finally:
        (store.data_dir / "service.json").unlink(missing_ok=True)
        store.close()
