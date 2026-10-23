"""Managed-launcher entry point. Importing this module does not start the app."""

from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .api import create_app
from .config import data_root
from .service import Service


def serve(port: int = 0, open_browser: bool = True):
    root = data_root()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        if not port:
            raise
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    actual_port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{actual_port}"
    server = None

    def quit_server():
        if server:
            server.should_exit = True

    application = create_app(Service(root), on_quit=quit_server)
    config = uvicorn.Config(application, log_level="warning", access_log=False, timeout_graceful_shutdown=15)
    server = uvicorn.Server(config)
    state_path = Path(os.getenv("GOOSNAV_RUNTIME_STATE") or root / "runtime" / "server.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(state_path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"schema_version": 1, "pid": os.getpid(), "url": url}), encoding="utf-8")
    temporary.replace(state_path)
    print(f"Auto Outbound: {url}", flush=True)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        try:
            if json.loads(state_path.read_text()).get("pid") == os.getpid():
                state_path.unlink()
        except (OSError, ValueError):
            pass


def main():
    # The native supervisor opens the browser immediately and redirects on readiness.
    supervised = bool(os.getenv("GOOSNAV_LAUNCH_SESSION"))
    serve(int(os.getenv("GOOSNAV_PORT_PREFERENCE", "0")), open_browser=not supervised)


if __name__ == "__main__":
    main()
