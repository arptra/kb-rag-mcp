"""Standalone helpers executed with Python -I -S in two separate containers."""

from __future__ import annotations

import base64
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from urllib.parse import urlsplit


def maven_path(path: str) -> bool:
    """Only Maven layout reads under the saved registry prefix, including metadata."""
    if len(path) > 2048 or not path.startswith("/"):
        return False
    parts = path[1:].split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", p) for p in parts):
        return False
    filename = re.sub(r"\.(?:sha1|sha256|sha512|md5|asc)$", "", parts[-1])
    if filename == "maven-metadata.xml":
        return True
    return (len(parts) >= 4 and filename.startswith(parts[-3] + "-")
            and filename.endswith((".pom", ".module", ".jar", ".war", ".zip", ".aar",
                                   ".klib", ".toml", ".so", ".tar.gz")))


class MavenRequest(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 20

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.serve()

    def do_GET(self):
        self.serve()

    def serve(self):
        if not maven_path(self.path):
            self.send_error(404)
            return
        conn = self.server.connection()
        try:
            conn.request(self.command, self.server.prefix + self.path, headers=self.server.headers)
            response = conn.getresponse()
            # Never forward authentication challenges, redirects, error bodies, or request headers.
            if response.status != 200:
                self.send_error(response.status if response.status in {401, 403, 404} else 502)
                return
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > self.server.max_bytes
            ):
                self.send_error(413)
                return
            self.send_response(200)
            chunked = length is None and self.command == "GET"
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif length is not None:
                self.send_header("Content-Length", length)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command == "GET":
                remaining = self.server.max_bytes
                while remaining > 0 and (chunk := response.read(min(65536, remaining))):
                    if chunked:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk)
                    if chunked:
                        self.wfile.write(b"\r\n")
                    remaining -= len(chunk)
                # Closing without a terminator makes a too-large response fail resolution.
                if chunked and not response.read(1):
                    self.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            if not getattr(self, "_sent_headers", False):
                self.send_error(502)
        finally:
            conn.close()

    def end_headers(self):
        super().end_headers()
        self._sent_headers = True


class UnixMavenServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, *args):
        pass


class SocketConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def upstream(path, limit, lifetime):
    # Only this process receives credentials. Its isolated interpreter imports no checkout code.
    credentials = json.load(sys.stdin)
    url = urlsplit(credentials["url"])
    auth = (credentials["username"] + ":" + credentials["password"]).encode()
    with UnixMavenServer(path, MavenRequest) as server:
        os.chmod(path, 0o600)
        server.headers = {"Authorization": "Basic " + base64.b64encode(auth).decode()}
        server.prefix = url.path.rstrip("/")
        server.max_bytes = limit
        server.connection = lambda: http.client.HTTPSConnection(url.hostname, url.port, timeout=20)
        # Expire even if the Docker client loses its route while removing the container.
        end = time.monotonic() + lifetime
        server.timeout = min(lifetime, 1)
        while time.monotonic() < end:
            server.handle_request()


INIT_SCRIPT = """
def localRepo = { handler ->
    handler.maven {
        url = uri('http://127.0.0.1:PORT/')
        allowInsecureProtocol = true
    }
}
def projectRepos = true
gradle.beforeSettings { s ->
    localRepo(s.pluginManagement.repositories)
    localRepo(s.dependencyResolutionManagement.repositories)
}
gradle.settingsEvaluated { s ->
    def mode = s.dependencyResolutionManagement.repositoriesMode.get()
    projectRepos = mode.name() == 'PREFER_PROJECT'
}
gradle.beforeProject { p ->
    localRepo(p.buildscript.repositories)
    if (projectRepos) localRepo(p.repositories)
}
"""


def build(path, limit, lifetime, command):
    # The bridge has no external network, auth header, registry URL, or secret material.
    end = time.monotonic() + lifetime
    while not os.path.exists(path):
        if time.monotonic() >= end:
            sys.exit("Maven registry connection could not start")
        time.sleep(0.05)
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), MavenRequest) as server:
        server.headers, server.prefix, server.max_bytes = {}, "", limit
        server.connection = lambda: SocketConnection(path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.NamedTemporaryFile(suffix=".init.gradle", mode="w") as script:
            script.write(INIT_SCRIPT.replace("PORT", str(server.server_port)))
            script.flush()
            os.environ.setdefault("GRADLE_USER_HOME", "/workspace/gradle-home")
            if not shutil.which(command[0]):
                sys.exit("Gradle executable is missing from the selected image or checkout")
            result = subprocess.run([command[0], "--no-daemon", "--init-script", script.name,
                                     *command[1:]], check=False)
            sys.exit(result.returncode)


if __name__ == "__main__":
    role, address, size, seconds, *arguments = sys.argv[1:]
    if role == "registry":
        upstream(address, int(size), float(seconds))
    elif role == "build":
        try:
            build(address, int(size), float(seconds), arguments)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(address)
