"""Read-only HTTP dashboard server. GET only, fixed routes, no access log.

Never imports probe or notify: it only reads the history database.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from resourcemonitor.paths import DEFAULT_HISTORY, DEFAULT_POLICY
from resourcemonitor.policy import load_policy
from resourcemonitor.queries import NoHistory, latest, open_ro, timeline, timeseries, usage

WEB_DIR = Path(__file__).parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_USAGE_DAYS = 366
# The scan-heavy endpoints share two slots, so a burst of page loads cannot pile up
# SQLite scans on the shared box; a request that waits longer than this gets 503 busy.
_HEAVY = frozenset({"/api/timeline", "/api/usage", "/api/timeseries"})
_QUERY_SLOTS = threading.BoundedSemaphore(2)
_BUSY_WAIT_S = 2


class BadRequest(ValueError):
    pass


def _params(query: str, allowed: set[str]) -> dict[str, str]:
    try:
        raw = parse_qs(query, keep_blank_values=True, strict_parsing=bool(query))
    except ValueError:
        raise BadRequest("query")
    if set(raw) - allowed or any(len(v) != 1 for v in raw.values()):
        raise BadRequest("query")
    return {k: v[0] for k, v in raw.items()}


def _hours(p: dict[str, str], default: int, hi: int) -> int:
    s = p.get("hours")
    if s is None:
        return default
    if not (s.isascii() and s.isdigit()):
        raise BadRequest("hours")
    h = int(s)
    if not 1 <= h <= hi:
        raise BadRequest("hours")
    return h


def _day(s: str) -> date:
    if len(s) != 10:
        raise BadRequest("date")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise BadRequest("date")


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    server_version = "resourcemonitor"
    timeout = 15                             # drop idle or slow-loris connections
    sys_version = ""

    def log_message(self, format, *args):   # no access logging
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if status != 204:                    # a 204 carries no body and no length
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _method_not_allowed(self) -> None:
        self._json(405, {"error": "method not allowed"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        try:
            if path in STATIC:
                name, ctype = STATIC[path]
                self._send(200, (WEB_DIR / name).read_bytes(), ctype)
                return
            if path == "/favicon.ico":       # browsers ask regardless; the page links the SVG
                self._send(204, b"", "text/plain")
                return
            handler = {"/api/now": self._now, "/api/usage": self._usage,
                       "/api/timeseries": self._timeseries, "/api/timeline": self._timeline,
                       "/healthz": self._healthz}.get(path)
            if handler is None:
                self._json(404, {"error": "not found"})
                return
            if path not in _HEAVY:
                handler(parts.query)
                return
            slots = _QUERY_SLOTS
            if not slots.acquire(timeout=_BUSY_WAIT_S):
                self._json(503, {"error": "busy"})
                return
            try:
                handler(parts.query)
            finally:
                slots.release()
        except BadRequest:
            self._json(400, {"error": "bad request"})
        except NoHistory:
            if path == "/healthz":
                self._json(503, {"ok": False})
            else:
                self._json(503, {"error": "no history yet"})
        except Exception as e:
            print(f"request failed: {e.__class__.__name__}", flush=True)
            self._json(500, {"error": "internal"})

    def _conn(self) -> sqlite3.Connection:
        return open_ro(self.server.history_path)

    def _assignments(self) -> dict[str, frozenset[int]]:
        try:
            return load_policy(self.server.policy_path).assignments
        except Exception:        # unreadable or invalid policy: show GPUs unassigned
            return {}

    def _now(self, query: str) -> None:
        _params(query, set())
        assignments = self._assignments()
        conn = self._conn()
        try:
            data = latest(conn, assignments, self.server.clock(), self.server.stale_after_s)
        finally:
            conn.close()
        self._json(200, data)

    def _healthz(self, query: str) -> None:
        _params(query, set())
        conn = self._conn()
        try:
            data = latest(conn, {}, self.server.clock(), self.server.stale_after_s)
        finally:
            conn.close()
        self._json(200, {"ok": True, "age_s": data["age_s"]})

    def _usage(self, query: str) -> None:
        p = _params(query, {"from", "to", "by"})
        now = self.server.clock()
        end = _day(p["to"]) if "to" in p else now.astimezone(timezone.utc).date()
        start = _day(p["from"]) if "from" in p else end - timedelta(days=13)
        by = p.get("by", "day")
        if by not in ("day", "week") or start > end \
                or (end - start).days + 1 > MAX_USAGE_DAYS:
            raise BadRequest("range")
        conn = self._conn()
        try:
            data = usage(conn, start, end, by, now)
        finally:
            conn.close()
        self._json(200, data)

    def _timeseries(self, query: str) -> None:
        p = _params(query, {"hours"})
        hours = _hours(p, 24, 168)
        conn = self._conn()
        try:
            data = timeseries(conn, hours, self.server.clock())
        finally:
            conn.close()
        self._json(200, data)

    def _timeline(self, query: str) -> None:
        p = _params(query, {"hours"})
        hours = _hours(p, 24, 720)
        conn = self._conn()
        try:
            data = timeline(conn, hours, self.server.clock())
        finally:
            conn.close()
        self._json(200, data)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    history_path: Path
    policy_path: Path
    stale_after_s: int
    clock: object


def make_server(bind: str, port: int, history_path: Path, policy_path: Path,
                stale_after_s: int = 180,
                clock=lambda: datetime.now(timezone.utc)) -> ThreadingHTTPServer:
    srv = _Server((bind, port), _Handler)
    srv.history_path = Path(history_path)
    srv.policy_path = Path(policy_path)
    srv.stale_after_s = stale_after_s
    srv.clock = clock
    return srv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resourcemonitor serve",
                                description="Read-only GPU dashboard over the history file.")
    p.add_argument("--bind", default="127.0.0.1", help="address to listen on")
    p.add_argument("--port", type=int, default=8765, help="TCP port")
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                   help="SQLite history file written by `watch` (opened read-only)")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--stale-after", type=int, default=180,
                   help="seconds without a poll before the page shows 'stale'")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    srv = make_server(args.bind, args.port, args.history, args.policy,
                      stale_after_s=args.stale_after)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
