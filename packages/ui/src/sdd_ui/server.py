"""Loopback-only operator UI with same-origin, token-protected mutations."""

import hashlib
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from sdd_core.codec import canonical, object_json
from sdd_core.ports import Conflict
from sdd_runtime.lock import Lease
from sdd_runtime.platform import NO_WINDOW

from sdd_ui.service import WorkspaceService

STATIC = files("sdd_ui").joinpath("static")
MIME = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}


def static_asset(path: str) -> tuple[bytes, str] | None:
    """A packaged UI file by URL path; only known types, no traversal outside `static`."""
    name = "index.html" if path == "/" else path.lstrip("/")
    parts = name.split("/")
    suffix = Path(name).suffix
    if suffix not in MIME or any(part in ("", ".", "..") for part in parts):
        return None
    resource = STATIC.joinpath(*parts)
    if not resource.is_file():
        return None
    return resource.read_bytes(), MIME[suffix] + "; charset=utf-8"


PACKAGES = (
    "sdd_core",
    "sdd_storage",
    "sdd_runtime",
    "sdd_providers",
    "sdd_workflows",
    "sdd_usage",
    "sdd_factory",
    "sdd_ui",
)


def code_stamp() -> float:
    """Newest modification time of the engine's Python code on disk.

    The UI's static files are read from disk on every request, but Python code
    runs as loaded at start: a newer stamp means a restart would apply an update.
    """
    newest = 0.0
    for name in PACKAGES:
        spec = find_spec(name)
        for folder in spec.submodule_search_locations or () if spec else ():
            for path in Path(folder).rglob("*.py"):
                newest = max(newest, path.stat().st_mtime)
    return newest


def relaunch_command(database: Path, config: Path | None, port: int) -> list[str]:
    """This same server as a fresh process: the current interpreter, no browser tab."""
    configured = "None" if config is None else f"Path({str(config)!r})"
    code = (
        "import sys; from pathlib import Path; from sdd_ui.server import launch; "
        f"sys.exit(launch(Path({str(database)!r}), {configured}, {port}, False))"
    )
    return [sys.executable, "-c", code]


def create_server(service: WorkspaceService, port: int) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)
    started = time.time()
    loaded = code_stamp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(
            self,
            status: int,
            body: bytes,
            mime: str = "application/json; charset=utf-8",
            etag: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if etag is not None:
                self.send_header("ETag", etag)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def trusted(self, mutation: bool = False) -> bool:
            host = f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            return self.headers.get("Host") == host and (
                not mutation
                or (
                    self.headers.get("Origin") == f"http://{host}"
                    and secrets.compare_digest(self.headers.get("X-FFAI-Token", ""), token)
                )
            )

        def do_GET(self) -> None:
            if not self.trusted():
                self.reply(403, b"{}")
                return
            url = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                asset = static_asset(url.path)
                if asset is not None:
                    self.reply(200, *asset)
                    return
                # Reads use their own short transactions and never wait for a queue tick.
                result: object
                if url.path == "/api/info":
                    result = {
                        "application": "feature-factory-ai",
                        "database": str(service.engine.store.path),
                        "versions": service.versions,
                        "started": started,
                        # Code on disk newer than the running code: a restart applies it.
                        "stale": "stale" in query and code_stamp() > loaded,
                    }
                elif url.path == "/api/state":
                    body = canonical({**service.state(), "token": token}).encode()
                    # The board polls every few seconds; unchanged state costs no transfer.
                    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self.reply(304, b"", etag=etag)
                    else:
                        self.reply(200, body, etag=etag)
                    return
                elif url.path == "/api/definition":
                    result = service.definition(query["digest"])
                elif url.path == "/api/run":
                    result = service.detail(query["id"])
                elif url.path == "/api/log":
                    result = service.flight(
                        query.get("run", ""), query.get("level", ""), int(query.get("limit", 300))
                    )
                elif url.path == "/api/live":
                    result = service.live(query["id"])
                elif url.path == "/api/incident":
                    result = service.incident(query["id"])
                else:
                    self.reply(404, b"{}")
                    return
                self.reply(200, canonical(result).encode())
            except (ValueError, KeyError, OSError) as error:
                self.reply(400, canonical({"error": str(error)}).encode())

        def do_POST(self) -> None:
            if not self.trusted(mutation=True):
                self.reply(403, b"{}")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("Request must be between 1 byte and 1 MiB")
                action = self.path.removeprefix("/api/")
                document = object_json(self.rfile.read(length).decode("utf-8"))
                if action in ("shutdown", "restart"):
                    restart = action == "restart"
                    service.shutdown(keep_queue=restart)
                    self.server.restart_requested = restart  # type: ignore[attr-defined]
                    self.reply(200, canonical({"stopping": True, "restart": restart}).encode())
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                # The service takes the coordinator lock only for actions that need it;
                # slow probes (discovery, models, quotas) never freeze the queue or polling.
                result: object = service.mutate(action, document)
                self.reply(200, canonical(result).encode())
            except (ValueError, KeyError, OSError) as error:
                self.reply(
                    409 if isinstance(error, Conflict) else 400,
                    canonical({"error": str(error)}).encode(),
                )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def launch(database: Path, config: Path | None, port: int, open_browser: bool) -> int:
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with Lease(database.with_suffix(".coordinator.lock")):
        service = WorkspaceService(database, config)
        try:
            server = create_server(service, port)
        except BaseException:
            service.coordinator.close()
            raise
        worker = threading.Thread(target=service.work, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Feature Factory AI: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            service.close()
            worker.join(timeout=30)
            server.server_close()
        restart = bool(getattr(server, "restart_requested", False))
    if restart:
        # The lease and the port are free now: the new process takes both.
        subprocess.Popen(
            relaunch_command(database, config, port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=sys.platform != "win32",
        )
    return 0
