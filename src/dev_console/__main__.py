"""Serve the console off-thread; reserve the foreground terminal for native CLI."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from dev_console.controller import DevConsoleController
from dev_console.server import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone local developer console")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--gigacode", default=os.environ.get("KB_GIGACODE_COMMAND", "gigacode"))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Use a port between 1 and 65535")
    root = args.project.expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        parser.error("--project must be a Git development checkout")
    token = secrets.token_urlsafe(32)
    controller = DevConsoleController(root, command=args.gigacode)
    # Bind before printing a launch URL; a busy port must never open another service.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        config = uvicorn.Config(
            create_app(controller, token=token, port=args.port),
            host="127.0.0.1",
            port=args.port,
            log_level="error",
            access_log=False,
            proxy_headers=False,
        )
        server = uvicorn.Server(config)
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, name="dev-console-http", daemon=True
        )
        worker.start()
        deadline = time.monotonic() + 10
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            raise RuntimeError("Dev console HTTP server did not start")
        print(f"\nDev Console: http://127.0.0.1:{args.port}/#token={token}", flush=True)
        print(
            "Оставь этот терминал открытым: здесь появятся подтверждения GigaCode.\n"
            "План и проверки утверждаются в браузере. Правки файлов — в CLI.\n"
            "После работы GigaCode введи /quit для возврата в цикл.",
            flush=True,
        )
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            print(
                "Нет интерактивного терминала: доступны запись и просмотр; "
                "для исправлений перезапусти команду в обычном терминале.",
                flush=True,
            )
        try:
            while worker.is_alive():
                controller.run_next_repair(timeout=0.3)
        except KeyboardInterrupt:
            print("\nDev Console остановлена. Артефакты сохранены.", flush=True)
        finally:
            controller.close()
            server.should_exit = True
            worker.join(timeout=5)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Cannot start Dev Console: {exc}", file=sys.stderr)
        return 2
    finally:
        controller.close()
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
