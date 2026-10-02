"""Real stdio child process -> verified HTTPS enrollment -> remote MCP regression."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
import sse_starlette.sse as sse
import test_access_http as http_fixtures
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from corporate_kb.access.client import TOKEN_ENV
from corporate_kb.mcp.http_server import tls_uvicorn_config

# Reuse PKI/service setup while keeping the real child-process test independent.
pki = http_fixtures.pki
secured = http_fixtures.secured


@asynccontextmanager
async def _https_server(app, settings):
    previous_exit_flag = sse.AppStatus.should_exit
    previous_tasks = asyncio.all_tasks()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(10)
    config = uvicorn.Config(
        app, log_level="critical", lifespan="on", **tls_uvicorn_config(settings)
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if task.done():
                await task
                pytest.fail("TLS server exited before startup")
            if time.monotonic() > deadline:
                pytest.fail("TLS server failed to start")
            await asyncio.sleep(0.01)
        yield f"https://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            sock.close()
            # sse-starlette's watcher captures this real Uvicorn instance and can
            # set a process-global exit flag after it stops. Neither the watcher
            # nor that flag may leak into later in-process ASGI tests.
            watchers = [
                pending
                for pending in asyncio.all_tasks() - previous_tasks
                if getattr(pending.get_coro(), "cr_code", None) is sse._shutdown_watcher.__code__
            ]
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            sse.AppStatus.should_exit = previous_exit_flag


async def _run_connect(directory: Path, origin: str, config: Path, child_env: dict[str, str]):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "corporate_kb.access.client",
        "connect",
        "--server-url",
        origin,
        "--cert",
        str(directory / "client.pem"),
        "--key",
        str(directory / "client.key"),
        "--ca",
        str(directory / "ca.pem"),
        "--config",
        str(config),
        "--transport",
        "stdio",
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    return stdout.decode(), stderr.decode()


async def _use_real_proxy(config: Path, source_path: str, stderr_path: Path):
    entry = json.loads(config.read_text())["mcpServers"]["corporate-kb"]
    params = StdioServerParameters(
        command=entry["command"],
        args=entry["args"],
        env={
            **entry["env"],
            "PYTHONPATH": source_path,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    with stderr_path.open("w", encoding="utf-8") as error_log:
        async with (
            stdio_client(params, errlog=error_log) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "kb_search" in names
            assert "kb_stats" in names
            stats = await session.call_tool("kb_stats", {})
            assert not stats.isError
            assert stats.structuredContent["document_count"] == 1
    return stderr_path.read_text()


@pytest.mark.asyncio
async def test_actual_stdio_proxy_enrollment_tools_reuse_and_expiry_renewal(secured, pki):
    app, settings, store, _ = secured
    directory, _ = pki
    config = directory / "client-settings.json"
    config.write_text(json.dumps({"unrelated": True, "mcpServers": {"other": {"command": "keep"}}}))
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    child_env = {**os.environ, "PYTHONPATH": source_path, "PYTHONDONTWRITEBYTECODE": "1"}

    async with _https_server(app, settings) as origin:
        output, connect_errors = await _run_connect(directory, origin, config, child_env)
        first = json.loads(config.read_text())["mcpServers"]["corporate-kb"]["env"][TOKEN_ENV]
        assert first not in output + connect_errors
        assert len(store.list_tokens()["items"]) == 1

        initial_errors = await _use_real_proxy(
            config, source_path, directory / "initial-stderr.log"
        )
        current = json.loads(config.read_text())
        assert current["mcpServers"]["corporate-kb"]["env"][TOKEN_ENV] == first
        assert len(store.list_tokens()["items"]) == 1
        assert first not in initial_errors

        # Expire the test credential without waiting for the configured production TTL.
        with sqlite3.connect(settings.access_db_path) as database:
            database.execute("UPDATE user_tokens SET expires_at = ?", (int(time.time()) - 1,))
        assert store.verify_user_token(first) is None

        renewed_errors = await _use_real_proxy(
            config, source_path, directory / "renewed-stderr.log"
        )
        current = json.loads(config.read_text())
        second = current["mcpServers"]["corporate-kb"]["env"][TOKEN_ENV]
        assert second != first
        assert store.verify_user_token(second) is not None
        assert len(store.list_tokens()["items"]) == 2
        assert current["unrelated"] is True
        assert current["mcpServers"]["other"] == {"command": "keep"}
        assert first not in renewed_errors
        assert second not in renewed_errors

        store.logout_user(second)
        logout_errors = await _use_real_proxy(config, source_path, directory / "logout-stderr.log")
        current = json.loads(config.read_text())
        third = current["mcpServers"]["corporate-kb"]["env"][TOKEN_ENV]
        assert third != second
        assert store.verify_user_token(third) is not None
        assert second not in logout_errors
        assert third not in logout_errors

        active = next(
            token for token in store.list_tokens()["items"] if token["status"] == "active"
        )
        store.revoke_token(active["id"], actor="test-admin", reason="Replace device credential")
        revoked_errors = await _use_real_proxy(
            config, source_path, directory / "revoked-stderr.log"
        )
        current = json.loads(config.read_text())
        fourth = current["mcpServers"]["corporate-kb"]["env"][TOKEN_ENV]
        assert fourth != third
        user = store.verify_user_token(fourth)
        assert user is not None
        assert third not in revoked_errors
        assert fourth not in revoked_errors
        assert len(store.list_tokens()["items"]) == 4

        # Unlike individual credential renewal, principal revocation must stop startup.
        store.revoke_user(user["id"], actor="test-admin", reason="Offboarding")
        config_before = config.read_bytes()
        entry = current["mcpServers"]["corporate-kb"]
        process = await asyncio.create_subprocess_exec(
            entry["command"],
            *entry["args"],
            env={**child_env, **entry["env"]},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 1
        assert stdout == b""
        assert "Access denied or revoked" in stderr.decode()
        assert fourth not in stderr.decode()
        assert config.read_bytes() == config_before
        assert len(store.list_tokens()["items"]) == 4
