"""Small local HTTP server for the interactive ApplyPilot dashboard."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from applypilot.view import (
    load_dashboard_data,
    mark_job_applied,
    mark_job_not_suitable,
    mark_job_saved,
    render_dashboard_html,
)


class DashboardHTTPServer(ThreadingHTTPServer):
    """HTTP server that tracks recent activity for idle shutdown."""

    def __init__(self, server_address, RequestHandlerClass, *, max_jobs: int, idle_seconds: int):
        super().__init__(server_address, RequestHandlerClass)
        self.max_jobs = max_jobs
        self.idle_seconds = idle_seconds
        self.last_request_ts = time.monotonic()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format, *args):  # noqa: A003
        return

    def _touch(self) -> None:
        self.server.last_request_ts = time.monotonic()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        self._touch()
        if self.path in ("/", "/index.html"):
            html = render_dashboard_html(load_dashboard_data(max_jobs=self.server.max_jobs), api_enabled=True)
            self._send_html(html)
            return
        if self.path == "/health":
            self._send_json({"ok": True})
            return
        self._send_json({"ok": False, "error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        self._touch()
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid_json"}, status=400)
            return

        if self.path == "/api/jobs/todo":
            url = str(payload.get("url") or "").strip()
            saved = bool(payload.get("saved"))
            if not url:
                self._send_json({"ok": False, "error": "missing_url"}, status=400)
                return
            saved_at = mark_job_saved(url, saved=saved)
            self._send_json({"ok": True, "saved_at": saved_at or ""})
            return

        if self.path == "/api/jobs/applied":
            url = str(payload.get("url") or "").strip()
            if not url:
                self._send_json({"ok": False, "error": "missing_url"}, status=400)
                return
            applied_at = mark_job_applied(url)
            self._send_json({"ok": True, "applied_at": applied_at})
            return

        if self.path == "/api/jobs/not-suitable":
            url = str(payload.get("url") or "").strip()
            not_suitable = bool(payload.get("not_suitable"))
            if not url:
                self._send_json({"ok": False, "error": "missing_url"}, status=400)
                return
            marked_at = mark_job_not_suitable(url, not_suitable=not_suitable)
            self._send_json({"ok": True, "not_suitable": not_suitable, "not_suitable_at": marked_at or ""})
            return

        self._send_json({"ok": False, "error": "not_found"}, status=404)


def _idle_shutdown(server: DashboardHTTPServer) -> None:
    while True:
        time.sleep(5)
        if time.monotonic() - server.last_request_ts > server.idle_seconds:
            server.shutdown()
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the interactive ApplyPilot dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--idle-seconds", type=int, default=1800)
    args = parser.parse_args()

    server = DashboardHTTPServer(
        (args.host, args.port),
        DashboardHandler,
        max_jobs=args.max_jobs,
        idle_seconds=args.idle_seconds,
    )
    monitor = threading.Thread(target=_idle_shutdown, args=(server,), daemon=True)
    monitor.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
