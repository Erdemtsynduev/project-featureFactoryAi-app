"""Loopback-only operator UI with same-origin, token-protected mutations."""

import hashlib
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from sdd_core.codec import canonical, object_json
from sdd_core.ports import Conflict
from sdd_runtime.lock import Lease

from sdd_ui.service import WorkspaceService


def create_server(service: WorkspaceService, port: int) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)

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
            try:
                if url.path in (
                    "/",
                    "/app.js",
                    "/style.css",
                    "/preferences.js",
                    "/locale.js",
                    "/workspace.js",
                    "/workspace.css",
                    "/pipeline.js",
                    "/office.js",
                    "/nav.js",
                    "/persist.js",
                    "/onboarding.js",
                ):
                    name = "index.html" if url.path == "/" else url.path[1:]
                    mime = (
                        "text/html"
                        if name.endswith(".html")
                        else "text/javascript"
                        if name.endswith(".js")
                        else "text/css"
                    ) + "; charset=utf-8"
                    self.reply(200, files("sdd_ui").joinpath("static", name).read_bytes(), mime)
                    return
                # Reads use their own short transactions and never wait for a queue tick.
                result: dict[str, object]
                if url.path == "/api/info":
                    result = {
                        "application": "feature-factory-ai",
                        "database": str(service.engine.store.path),
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
                elif url.path == "/api/run":
                    result = service.detail(parse_qs(url.query)["id"][0])
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
                if action == "shutdown":
                    with service.lock:
                        if service.coordinator.live:
                            raise Conflict(
                                "Pause the queue and wait for active executions before closing"
                            )
                        service.mutate("queue", {"running": False})
                    self.reply(200, b'{"stopping":true}')
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
        return 0
