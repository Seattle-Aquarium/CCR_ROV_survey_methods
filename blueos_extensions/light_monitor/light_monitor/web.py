"""
The extension's web side: its page, its API, and what BlueOS needs to list it.

==============================  ===============================================
GET  /                          the page (static/index.html)
GET  /register_service          what BlueOS shows in its menu
GET  /api/v1/state              everything, as one JSON document
                                (`model.Model.snapshot`, schema
                                light_monitor.state/1) -- what the page and
                                rov_flight_ops both read
GET  /api/v1/records?after=N    the recording's records with id > N, oldest
                                first, at most 2000; `rov_flight_ops` keeps
                                a copy of the recording on the topside this way
GET  /api/v1/files              the recording files on the Pi
GET  /files/<name>              one of them, to download
POST /api/v1/port/release       {"minutes": 15} -- close the controller's port
                                so mpremote can use it; it reopens by itself
POST /api/v1/port/resume        reopen it now
==============================  ===============================================

Every path is relative in the page, so it works both at its own port and
behind BlueOS's proxy (`/extensionv2/<name>/`), on 1.4 and 1.5.

The two POSTs act on the extension only -- the light controller is never
written to by anything here.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
_SAFE_NAME = re.compile(r"^lights_[0-9_]+(?:_1hz)?\.(?:jsonl|csv)$")


class Api:
    """What the handler needs from the application -- kept narrow on purpose."""

    def state(self) -> dict: ...
    def records(self, after: int, limit: int) -> tuple[list[dict], int]: ...
    def files(self) -> list[dict]: ...
    def file_path(self, name: str) -> Path | None: ...
    def release(self, minutes: float) -> dict: ...
    def resume(self) -> dict: ...
    def service(self) -> dict: ...


def make_handler(api: Api):

    class Handler(BaseHTTPRequestHandler):
        server_version = "LightMonitor/1"

        def log_message(self, fmt, *args):            # quiet: BlueOS polls often
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- helpers ----------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, separators=(",", ":"),
                                          default=str).encode("utf-8"),
                       "application/json")

        def _static(self, rel: str):
            p = (STATIC / rel).resolve()
            if STATIC not in p.parents or not p.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if p.suffix == ".ttf":
                ctype = "font/ttf"
            self._send(200, p.read_bytes(), ctype)

        # -- routes -----------------------------------------------------
        def do_GET(self):                              # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            q = urllib.parse.parse_qs(url.query)
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/register_service":
                    return self._json(api.service())
                if path == "/api/v1/state":
                    return self._json(api.state())
                if path == "/api/v1/records":
                    after = int((q.get("after") or ["0"])[0])
                    limit = max(1, min(5000, int((q.get("limit") or ["2000"])[0])))
                    items, newest = api.records(after, limit)
                    return self._json({"records": items, "newest": newest})
                if path == "/api/v1/files":
                    return self._json({"files": api.files()})
                if path.startswith("/files/"):
                    name = urllib.parse.unquote(path[len("/files/"):])
                    if not _SAFE_NAME.match(name):
                        return self._json({"error": "not a recording"}, 404)
                    p = api.file_path(name)
                    if p is None or not p.is_file():
                        return self._json({"error": "not found"}, 404)
                    ctype = "text/csv" if name.endswith(".csv") else "application/x-ndjson"
                    return self._send(200, p.read_bytes(), ctype, {
                        "Content-Disposition": f'attachment; filename="{name}"'})
                if path == "/api/v1/health":
                    return self._json({"ok": True})
                return self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as ex:                    # noqa: BLE001
                log.exception("GET %s", self.path)
                try:
                    return self._json({"error": str(ex)}, 500)
                except Exception:
                    return None

        def do_HEAD(self):                             # noqa: N802
            return self.do_GET()

        def do_POST(self):                             # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                body = {}
            if path == "/api/v1/port/release":
                minutes = float(body.get("minutes", 15)) if isinstance(body, dict) else 15.0
                return self._json(api.release(max(0.5, min(120.0, minutes))))
            if path == "/api/v1/port/resume":
                return self._json(api.resume())
            return self._json({"error": "not found"}, 404)

        def do_OPTIONS(self):                          # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    return Handler


def serve(api: Api, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(api))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    return server
