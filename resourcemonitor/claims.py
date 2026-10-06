"""The only module that writes claims.sqlite (GPU bookings). Rows are never deleted:
cancelling sets cancelled_at/cancelled_ip, so the table is the audit trail.
The monitor reads bookings with load_active (mode=ro); it never enforces them."""
from __future__ import annotations

import pwd
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

DEFAULT_CLAIMS = Path.home() / ".local/state/resourcemonitor/claims.sqlite"
MIB_PER_GIB = 1024
DEFAULT_CARD_MIB = 81559
MAX_DAYS = 14
PAST_SLACK = timedelta(minutes=5)
NOTE_MAX = 120
GPU_COUNT = 8
SCHEMA_VERSION = 1

USER_RE = re.compile(r"^[a-z_][a-z0-9._-]{0,31}$")
_NOBODY_UID = 65534
_TIME_FMT = "%a %d %b %H:%M UTC"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  id           INTEGER PRIMARY KEY,
  user         TEXT    NOT NULL,
  gpu          INTEGER NOT NULL CHECK (gpu BETWEEN 0 AND 7),
  vram_mib     INTEGER NOT NULL CHECK (vram_mib > 0),
  start        TEXT    NOT NULL,
  end          TEXT    NOT NULL,
  note         TEXT,
  created_at   TEXT    NOT NULL,
  created_ip   TEXT    NOT NULL,
  cancelled_at TEXT,
  cancelled_ip TEXT
);
CREATE INDEX IF NOT EXISTS claims_gpu_window ON claims(gpu, start, end);
"""

_COLUMNS = ("id, user, gpu, vram_mib, start, end, note, created_at, created_ip, "
            "cancelled_at, cancelled_ip")


@dataclass(frozen=True)
class Booking:
    id: int
    user: str
    gpu: int
    vram_mib: int
    start: datetime
    end: datetime
    note: str | None
    created_at: datetime
    created_ip: str
    cancelled_at: datetime | None = None
    cancelled_ip: str | None = None

    def active_at(self, t: datetime) -> bool:
        return self.cancelled_at is None and self.start <= t < self.end

    def to_json(self) -> dict:
        def iso(d: datetime | None) -> str | None:
            return None if d is None else d.isoformat()
        return {
            "id": self.id, "user": self.user, "gpu": self.gpu,
            "vram_mib": self.vram_mib, "vram_gib": round(self.vram_mib / MIB_PER_GIB, 1),
            "start": iso(self.start), "end": iso(self.end), "note": self.note,
            "created_at": iso(self.created_at), "created_ip": self.created_ip,
            "cancelled_at": iso(self.cancelled_at), "cancelled_ip": self.cancelled_ip,
        }


class ClaimError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class UserDirectory(Protocol):
    def known(self, name: str) -> bool: ...
    def all(self) -> list[str]: ...


class PwdUsers:
    """Real login accounts: pwd entries with uid >= min_uid and a valid name."""

    def __init__(self, min_uid: int = 1000):
        self.min_uid = min_uid

    def _real(self, name: str, uid: int) -> bool:
        return uid >= self.min_uid and uid != _NOBODY_UID and bool(USER_RE.fullmatch(name))

    def known(self, name: str) -> bool:
        try:
            return self._real(name, pwd.getpwnam(name).pw_uid)
        except KeyError:
            return False

    def all(self) -> list[str]:
        return sorted({p.pw_name for p in pwd.getpwall() if self._real(p.pw_name, p.pw_uid)})


def parse_time(s: str) -> datetime:
    """ISO-8601 with an offset -> aware UTC datetime; anything else is a 400."""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        d = None
    if d is None or d.tzinfo is None:
        raise ClaimError(400, "times need a date, time and UTC offset")
    return d.astimezone(timezone.utc)


def _utc(d: datetime) -> datetime:
    if d.tzinfo is None:
        raise ValueError("naive datetime")
    return d.astimezone(timezone.utc)


def _ts(d: datetime | None) -> datetime | None:
    return None if d is None else datetime.fromisoformat(d)


def _row(r: tuple) -> Booking:
    return Booking(id=r[0], user=r[1], gpu=r[2], vram_mib=r[3], start=_ts(r[4]),
                   end=_ts(r[5]), note=r[6], created_at=_ts(r[7]), created_ip=r[8],
                   cancelled_at=_ts(r[9]), cancelled_ip=r[10])


def _validate(users: UserDirectory, user: str, gpu: int, vram_mib: int, start: datetime,
              end: datetime, note: str | None, now: datetime, card_mib: int) -> str | None:
    """Raise ClaimError(400) on the first broken rule; return the note to store."""
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ClaimError(400, "invalid user name")
    if not users.known(user):
        raise ClaimError(400, f"unknown user '{user}'")
    if not 0 <= gpu < GPU_COUNT:
        raise ClaimError(400, "GPU must be 0–7")
    if vram_mib < MIB_PER_GIB or vram_mib > card_mib:
        raise ClaimError(400, f"VRAM must be between 1 and {card_mib // MIB_PER_GIB} GiB")
    if end <= start:
        raise ClaimError(400, "end must be after start")
    if start < now - PAST_SLACK:
        raise ClaimError(400, "start lies in the past")
    if end - start > timedelta(days=MAX_DAYS):
        raise ClaimError(400, f"a booking may last at most {MAX_DAYS} days")
    if note is None:
        return None
    if len(note) > NOTE_MAX:
        raise ClaimError(400, "note too long")
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in note):
        raise ClaimError(400, "invalid note")
    return note if note.strip() else None


def _check_capacity(overlapping: list[Booking], gpu: int, vram_mib: int, start: datetime,
                    end: datetime, card_mib: int) -> None:
    """Booked VRAM only rises at a booking's start, so checking the new window's start
    and every overlapping start inside it covers the worst instant."""
    instants = sorted({start} | {b.start for b in overlapping if start < b.start < end})
    for t in instants:
        active = [b for b in overlapping if b.active_at(t)]
        booked = sum(b.vram_mib for b in active)
        if booked + vram_mib > card_mib:
            t2 = min(min(b.end for b in active), end)
            free = (card_mib - booked) / MIB_PER_GIB
            raise ClaimError(409, f"GPU {gpu} has only {free:.1f} GiB unbooked between "
                                  f"{t.strftime(_TIME_FMT)} and {t2.strftime(_TIME_FMT)}")


class ClaimsStore:
    def __init__(self, path: Path | str, users: UserDirectory | None = None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.users: UserDirectory = users if users is not None else PwdUsers()
        self._lock = threading.Lock()
        # Autocommit mode: transactions are opened explicitly with BEGIN IMMEDIATE.
        self._conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False,
                                     isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self._conn.close()
            raise

    def create(self, *, user: str, gpu: int, vram_mib: int, start: datetime, end: datetime,
               note: str | None, ip: str, now: datetime,
               card_mib: int = DEFAULT_CARD_MIB) -> Booking:
        start, end, now = _utc(start), _utc(end), _utc(now)
        note = _validate(self.users, user, gpu, vram_mib, start, end, note, now, card_mib)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                overlapping = [_row(r) for r in conn.execute(
                    f"SELECT {_COLUMNS} FROM claims WHERE gpu = ? AND cancelled_at IS NULL "
                    "AND start < ? AND end > ?", (gpu, end.isoformat(), start.isoformat()))]
                _check_capacity(overlapping, gpu, vram_mib, start, end, card_mib)
                cur = conn.execute(
                    "INSERT INTO claims (user, gpu, vram_mib, start, end, note, created_at, "
                    "created_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user, gpu, vram_mib, start.isoformat(), end.isoformat(), note,
                     now.isoformat(), ip))
                claim_id = cur.lastrowid
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return Booking(id=claim_id, user=user, gpu=gpu, vram_mib=vram_mib, start=start,
                       end=end, note=note, created_at=now, created_ip=ip)

    def cancel(self, claim_id: int, *, ip: str, now: datetime) -> Booking:
        """Idempotent: cancelling an already-cancelled booking changes nothing."""
        now = _utc(now)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                                "WHERE id = ? AND cancelled_at IS NULL",
                                (now.isoformat(), ip, claim_id))
                row = conn.execute(f"SELECT {_COLUMNS} FROM claims WHERE id = ?",
                                      (claim_id,)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if row is None:
            raise ClaimError(404, "no such booking")
        return _row(row)

    def get(self, claim_id: int) -> Booking | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM claims WHERE id = ?",
                                     (claim_id,)).fetchone()
        return None if row is None else _row(row)

    def list_window(self, now: datetime, days_ahead: int = 14,
                    days_back: int = 7) -> list[Booking]:
        """All rows, cancelled included, touching [now - days_back, now + days_ahead]."""
        now = _utc(now)
        lo = (now - timedelta(days=days_back)).isoformat()
        hi = (now + timedelta(days=days_ahead)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM claims WHERE end >= ? AND start <= ? "
                "ORDER BY start, id", (lo, hi)).fetchall()
        return [_row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _read_ro(path: Path | str, sql: str, params: tuple) -> list[Booking]:
    """Run a SELECT of _COLUMNS rows with mode=ro. Fail-open: a missing, corrupt, locked or
    malformed file means no bookings, and the error is printed by exception class only."""
    try:
        path = Path(path)
        if not path.exists():
            return []
        # as_uri() percent-encodes '?', '#' and '%', so they cannot leak into the query string.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row(r) for r in rows]
    except Exception as e:
        print(f"claims unavailable: {type(e).__name__}")
        return []


def load_active(path: Path | str, t: datetime) -> list[Booking]:
    """Bookings active at t, read with mode=ro (fail-open, see _read_ro)."""
    try:
        ts = _utc(t).isoformat()
    except Exception as e:
        print(f"claims unavailable: {type(e).__name__}")
        return []
    return _read_ro(path, f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL "
                    "AND start <= ? AND end > ? ORDER BY gpu, start, id", (ts, ts))


def list_window_ro(path: Path | str, now: datetime, days_ahead: int = 14,
                   days_back: int = 7) -> list[Booking]:
    """Like ClaimsStore.list_window, but read with mode=ro and fail-open."""
    try:
        now = _utc(now)
        lo = (now - timedelta(days=days_back)).isoformat()
        hi = (now + timedelta(days=days_ahead)).isoformat()
    except Exception as e:
        print(f"claims unavailable: {type(e).__name__}")
        return []
    return _read_ro(path, f"SELECT {_COLUMNS} FROM claims WHERE end >= ? AND start <= ? "
                    "ORDER BY start, id", (lo, hi))
