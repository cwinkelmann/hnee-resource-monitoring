"""HTTP dashboard server: fixed routes, no access log. GETs read the history and
claims databases with mode=ro; the only writes are bookings (POST /api/claims,
/api/claims/quick and /api/claims/<id>/cancel) into claims.sqlite, behind drive-by protections.

Never imports probe, notify or cli.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from resourcemonitor.claims import (DEFAULT_CARD_MIB, DEFAULT_CLAIMS, GPU_COUNT, MIB_PER_GIB,
                                    ClaimError, ClaimsStore, PwdUsers, UserDirectory,
                                    list_window_ro, load_active, parse_time)
from resourcemonitor.paths import DEFAULT_HISTORY, DEFAULT_POLICY
from resourcemonitor.queries import (NoHistory, latest, open_ro, timeline, timeseries, usage,
                                     vram_timeseries)

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
_HEAVY = frozenset({"/api/timeline", "/api/usage", "/api/timeseries", "/api/vram"})
_QUERY_SLOTS = threading.BoundedSemaphore(2)
_BUSY_WAIT_S = 2

MAX_BODY = 4096
_DRAIN_MAX = 64 * 1024                          # unread body bytes swallowed before a rejection
_DRAIN_TIMEOUT_S = 1
_CANCEL = re.compile(r"^/api/claims/(\d{1,9})/cancel$", re.ASCII)
_GET_ONLY = frozenset(STATIC) | {"/favicon.ico", "/api/now", "/api/usage", "/api/timeseries",
                                 "/api/vram", "/api/timeline", "/api/users", "/healthz"}
_CLAIM_FIELDS = ("user", "gpu", "vram_gib", "start", "end", "note")
_QUICK = "/api/claims/quick"
_QUICK_FIELDS = ("user", "gpu")
_CLAIM_ERRORS = {400: "invalid", 404: "not found", 409: "conflict"}


class _RateLimiter:
    """At most `limit` writes per client IP in any rolling `window_s` (in memory)."""

    def __init__(self, limit: int = 30, window_s: float = 3600):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str, now: float) -> bool:
        with self._lock:
            for key in [k for k, q in self._hits.items() if q[-1] <= now - self.window_s]:
                del self._hits[key]                 # forget clients idle for a whole window
            q = self._hits.setdefault(ip, deque())
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


_WRITES = _RateLimiter()


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


class _Reject(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(status)
        self.status = status
        self.body = body


def _field(d: dict, name: str, ok) -> object:
    if name not in d or not ok(d[name]):
        raise ClaimError(400, f"missing or invalid field '{name}'")
    return d[name]


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_gib(v) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v) and v > 0 and math.isfinite(v * MIB_PER_GIB)
    except OverflowError:                       # an int too large to convert to float
        return False


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

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

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
                       "/api/timeseries": self._timeseries, "/api/vram": self._vram,
                       "/api/timeline": self._timeline,
                       "/api/claims": self._claims, "/api/users": self._users,
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

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path in _GET_ONLY:
            self._drain()
            self._method_not_allowed()
            return
        m = _CANCEL.match(path)
        if path not in ("/api/claims", _QUICK) and m is None:
            self._drain()
            self._json(404, {"error": "not found"})
            return
        try:
            body = self._write_body()
            if path == _QUICK:
                claim = self._quick(body)
                if claim is None:
                    self._json(200, {"claim": None})
                else:
                    self._json(201, {"claim": claim.to_json()})
            elif m is None:
                claim = self._create(body)
                self._json(201, {"claim": claim.to_json()})
            else:
                if body:
                    raise ClaimError(400, "unknown field")
                claim = self._store().cancel(int(m.group(1)), ip=self.client_address[0],
                                             now=self.server.clock())
                self._json(200, {"claim": claim.to_json()})
        except _Reject as e:
            self._drain()
            self._json(e.status, e.body)
        except ClaimError as e:
            self._json(e.status, {"error": _CLAIM_ERRORS[e.status], "detail": e.detail})
        except Exception as e:
            print(f"request failed: {e.__class__.__name__}", flush=True)
            self._json(500, {"error": "internal"})

    def _drain(self) -> None:
        """Swallow up to 64 KiB of an unread body (1 s in total), then close after replying.
        Closing a socket with unread input sends a TCP reset, which can make the client
        lose the response it was about to read."""
        self.close_connection = True
        length = self.headers.get("Content-Length", "")
        if not (length.isascii() and length.isdigit()) or len(length) > 12:
            return
        remaining = min(int(length), _DRAIN_MAX)
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S   # total, not per read: no slow-loris
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(max(left, 0.01))
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _write_body(self) -> dict:
        """Apply the drive-by protections in order, then read and parse the JSON body."""
        if not _WRITES.allow(self.client_address[0], time.monotonic()):
            raise _Reject(429, {"error": "too many requests"})
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + self.headers.get("Host", ""):
            raise _Reject(403, {"error": "forbidden"})
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _Reject(415, {"error": "unsupported media type"})
        length = self.headers.get("Content-Length", "")
        if not (length.isascii() and length.isdigit()) or len(length) > 4 \
                or int(length) > MAX_BODY:
            raise _Reject(413, {"error": "too large"})
        raw = self.rfile.read(int(length))
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            raise ClaimError(400, "body must be a JSON object")
        return body

    def _create(self, d: dict):
        if set(d) - set(_CLAIM_FIELDS):
            raise ClaimError(400, "unknown field")
        user = _field(d, "user", lambda v: isinstance(v, str))
        gpu = _field(d, "gpu", _is_int)
        # VRAM is optional: absent or null books the whole card.
        vram_gib = None if d.get("vram_gib") is None else _field(d, "vram_gib", _is_gib)
        start = parse_time(_field(d, "start", lambda v: isinstance(v, str)))
        end = parse_time(_field(d, "end", lambda v: isinstance(v, str)))
        note = d.get("note")
        if note is not None and not isinstance(note, str):
            raise ClaimError(400, "missing or invalid field 'note'")
        card_mib = self._card_mib(gpu)
        vram_mib = card_mib if vram_gib is None else round(vram_gib * MIB_PER_GIB)
        return self._store().create(user=user, gpu=gpu, vram_mib=vram_mib,
                                    start=start, end=end, note=note,
                                    ip=self.client_address[0], now=self.server.clock(),
                                    card_mib=card_mib)

    def _quick(self, d: dict):
        if set(d) - set(_QUICK_FIELDS):
            raise ClaimError(400, "unknown field")
        user = _field(d, "user", lambda v: v is None or isinstance(v, str))
        gpu = _field(d, "gpu", _is_int)
        return self._store().quick(user=user, gpu=gpu, ip=self.client_address[0],
                                   now=self.server.clock(), card_mib=self._card_mib(gpu))

    def _card_mib(self, gpu: int) -> int:
        """The latest poll's total for this GPU; the default card size if there is none
        (or the GPU number is out of range, which the store then refuses with a 400)."""
        if not 0 <= gpu < GPU_COUNT:
            return DEFAULT_CARD_MIB
        try:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT total_mib FROM gpu_samples WHERE gpu = ? "
                    "AND ts = (SELECT MAX(ts) FROM polls)", (gpu,)).fetchone()
            finally:
                conn.close()
        except (NoHistory, sqlite3.Error):
            return DEFAULT_CARD_MIB
        return row[0] if row and row[0] else DEFAULT_CARD_MIB

    def _store(self) -> ClaimsStore:
        srv = self.server
        with srv.store_lock:
            if srv.store is None:
                srv.store = ClaimsStore(srv.claims_path, users=srv.users)
            return srv.store

    def _conn(self) -> sqlite3.Connection:
        return open_ro(self.server.history_path)

    def _now(self, query: str) -> None:
        _params(query, set())
        now = self.server.clock()
        bookings = load_active(self.server.claims_path, now)
        conn = self._conn()
        try:
            data = latest(conn, bookings, now, self.server.stale_after_s)
        finally:
            conn.close()
        self._json(200, data)

    def _healthz(self, query: str) -> None:
        _params(query, set())
        conn = self._conn()
        try:
            data = latest(conn, [], self.server.clock(), self.server.stale_after_s)
        finally:
            conn.close()
        self._json(200, {"ok": True, "age_s": data["age_s"]})

    def _claims(self, query: str) -> None:
        p = _params(query, {"days", "back"})

        def small_int(name: str, default: int, hi: int) -> int:
            if name not in p:
                return default
            v = p[name]
            if len(v) > 2 or not (v.isascii() and v.isdigit()) or not 1 <= int(v) <= hi:
                raise BadRequest(name)
            return int(v)

        days = small_int("days", 14, 14)      # days ahead
        back = small_int("back", 7, 30)       # days back (the timeline's 30 d view needs 30)
        now = self.server.clock()
        claims = list_window_ro(self.server.claims_path, now, days_ahead=days, days_back=back)
        self._json(200, {"now": now.isoformat(), "claims": [b.to_json() for b in claims]})

    def _users(self, query: str) -> None:
        _params(query, set())
        self._json(200, {"users": self.server.users.all()})

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

    def _vram(self, query: str) -> None:
        p = _params(query, {"hours"})
        if len(p.get("hours", "")) > 3:
            raise BadRequest("hours")
        hours = _hours(p, 24, 168)
        conn = self._conn()
        try:
            data = vram_timeseries(conn, hours, self.server.clock())
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
    claims_path: Path
    users: UserDirectory
    store: ClaimsStore | None = None
    store_lock: threading.Lock

    def server_close(self) -> None:
        super().server_close()
        with self.store_lock:
            if self.store is not None:
                self.store.close()
                self.store = None


def make_server(bind: str, port: int, history_path: Path, policy_path: Path,
                stale_after_s: int = 180,
                clock=lambda: datetime.now(timezone.utc),
                claims_path: Path = DEFAULT_CLAIMS,
                users: UserDirectory | None = None) -> ThreadingHTTPServer:
    srv = _Server((bind, port), _Handler)
    srv.history_path = Path(history_path)
    srv.policy_path = Path(policy_path)
    srv.stale_after_s = stale_after_s
    srv.clock = clock
    srv.claims_path = Path(claims_path)
    srv.users = users if users is not None else PwdUsers()
    srv.store_lock = threading.Lock()                # the store itself opens on the first write
    return srv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resourcemonitor serve",
                                description="Read-only GPU dashboard over the history file.")
    p.add_argument("--bind", default="127.0.0.1", help="address to listen on")
    p.add_argument("--port", type=int, default=8765, help="TCP port")
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                   help="SQLite history file written by `watch` (opened read-only)")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS,
                   help="SQLite bookings file (read-only for GETs; written by booking POSTs)")
    p.add_argument("--stale-after", type=int, default=180,
                   help="seconds without a poll before the page shows 'stale'")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    srv = make_server(args.bind, args.port, args.history, args.policy,
                      stale_after_s=args.stale_after, claims_path=args.claims)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
