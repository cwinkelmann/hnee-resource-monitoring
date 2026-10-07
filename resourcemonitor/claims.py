"""The only module that writes claims.sqlite (GPU bookings). Rows are never deleted:
cancelling sets cancelled_at/cancelled_ip, so the table is the audit trail.
The monitor reads bookings with load_active (mode=ro); it never enforces them.

A booking is "important" or "lendable". allocate() decides each booking's effective share at
an instant: important ones in full, the rest of the card to lendable ones oldest first. What
an important booking takes from a lendable one is derived from the rows, never stored."""
from __future__ import annotations

import pwd
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

DEFAULT_CLAIMS = Path.home() / ".local/state/resourcemonitor/claims.sqlite"
MIB_PER_GIB = 1024
DEFAULT_CARD_MIB = 81559
MAX_DAYS = 14
PAST_SLACK = timedelta(minutes=5)
NOTE_MAX = 120
GPU_COUNT = 8
SCHEMA_VERSION = 3
QUICK_RESET_HOUR = 9
QUICK_TZ = "Europe/Berlin"
QUICK_NOTE = "quick booking"
QUICK_MIN = timedelta(minutes=1)
PRIORITIES = ("lendable", "important")
DEFAULT_GRACE = timedelta(minutes=30)

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
  cancelled_ip TEXT,
  kind         TEXT    NOT NULL DEFAULT 'calendar',
  priority     TEXT    NOT NULL DEFAULT 'important'
);
CREATE INDEX IF NOT EXISTS claims_gpu_window ON claims(gpu, start, end);
"""

_COLUMNS_V1 = ("id, user, gpu, vram_mib, start, end, note, created_at, created_ip, "
               "cancelled_at, cancelled_ip")
_COLUMNS = _COLUMNS_V1 + ", kind, priority"
# Older files read before the web process migrated them: version 1 has no kind (all
# calendar), version 2 no priority. Rows from before priorities existed blocked everyone,
# so they read as important.
_COLUMNS_V2_AS_V3 = _COLUMNS_V1 + ", kind, 'important'"
_COLUMNS_V1_AS_V3 = _COLUMNS_V1 + ", 'calendar', 'important'"


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
    kind: str = "calendar"                  # "calendar" (the form) or "quick" (a card's holder)
    priority: str = "lendable"              # "lendable" or "important"

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
            "kind": self.kind, "priority": self.priority,
        }


@dataclass(frozen=True)
class Created:
    """A new booking, the lendable bookings it takes from, and the start it asked for if the
    grace period moved it (else None)."""
    booking: Booking
    takes: list[dict]
    adjusted_start: datetime | None = None


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


def next_reset(now: datetime, hour: int = QUICK_RESET_HOUR, tz: str = QUICK_TZ) -> datetime:
    """The next local hour:00 in tz strictly after now, as aware UTC (DST-correct)."""
    now = _utc(now)
    zone = ZoneInfo(tz)
    day = now.astimezone(zone).date()
    for d in (day, day + timedelta(days=1)):
        t = datetime.combine(d, time(hour), tzinfo=zone).astimezone(timezone.utc)
        if t > now:                         # compared in UTC: same-zone compares ignore DST
            return t
    raise AssertionError("unreachable")


def _ts(d: datetime | None) -> datetime | None:
    return None if d is None else datetime.fromisoformat(d)


def _row(r: tuple) -> Booking:
    return Booking(id=r[0], user=r[1], gpu=r[2], vram_mib=r[3], start=_ts(r[4]),
                   end=_ts(r[5]), note=r[6], created_at=_ts(r[7]), created_ip=r[8],
                   cancelled_at=_ts(r[9]), cancelled_ip=r[10], kind=r[11],
                   priority=r[12])


def _validate_user(users: UserDirectory, user: str) -> None:
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ClaimError(400, "invalid user name")
    if not users.known(user):
        raise ClaimError(400, f"unknown user '{user}'")


def _validate_gpu(gpu: int) -> None:
    if not 0 <= gpu < GPU_COUNT:
        raise ClaimError(400, "GPU must be 0–7")


def _validate(users: UserDirectory, user: str, gpu: int, vram_mib: int, start: datetime,
              end: datetime, note: str | None, now: datetime, card_mib: int) -> str | None:
    """Raise ClaimError(400) on the first broken rule; return the note to store."""
    _validate_user(users, user)
    _validate_gpu(gpu)
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


def allocate(active: list[Booking], total_mib: int) -> dict[int, int]:
    """Effective MiB per booking id, for bookings active at one instant on one GPU.
    Important bookings get their full VRAM; what the card has left goes to lendable ones
    oldest (lowest id) first, so the newest lendable booking is the first to shrink."""
    out = {b.id: b.vram_mib for b in active if b.priority == "important"}
    left = total_mib - sum(out.values())
    for b in sorted((b for b in active if b.priority != "important"), key=lambda b: b.id):
        out[b.id] = max(0, min(b.vram_mib, left))
        left -= out[b.id]
    return out


def _spans(bookings: list[Booking], lo: datetime, hi: datetime):
    """Yield (start, end, active) over [lo, hi), cut wherever a booking starts or ends, so
    the set of active (uncancelled) bookings is constant inside each span."""
    live = [b for b in bookings if b.cancelled_at is None and b.start < hi and b.end > lo]
    cuts = sorted({lo, hi} | {t for b in live for t in (b.start, b.end) if lo < t < hi})
    for a, z in zip(cuts, cuts[1:]):
        yield a, z, [b for b in live if b.start <= a < b.end]


def _merge(segs: list[dict], seg: dict, same: tuple[str, ...]) -> None:
    """Append seg, or extend the last segment if it ends where seg starts and matches."""
    last = segs[-1] if segs else None
    if last and last["end"] == seg["start"] and all(last[k] == seg[k] for k in same):
        last["end"] = seg["end"]
    else:
        segs.append(seg)


def taken_segments(b: Booking, bookings: list[Booking], total_mib: int, lo: datetime,
                   hi: datetime) -> list[dict]:
    """Where lendable booking b gets less than it booked, within [lo, hi): merged segments
    {start, end, vram_mib taken, by: users of the important bookings active then}.
    `bookings` are other rows (any GPU; b itself may be among them)."""
    if b.priority == "important" or b.cancelled_at is not None:
        return []
    others = [o for o in bookings if o.gpu == b.gpu and o.id != b.id]
    segs: list[dict] = []
    for a, z, active in _spans(others + [b], max(lo, b.start), min(hi, b.end)):
        short = b.vram_mib - allocate(active, total_mib).get(b.id, b.vram_mib)
        if short > 0:
            by = sorted({o.user for o in active if o.priority == "important"})
            _merge(segs, {"start": a, "end": z, "vram_mib": short, "by": by}, ("vram_mib", "by"))
    return segs


def takes_of(new: Booking, existing: list[Booking], total_mib: int) -> list[dict]:
    """What `new` takes from lendable bookings: per squeezed booking, merged segments
    {id, user, gpu, vram_mib, start, end}, ordered by start then id."""
    others = [o for o in existing if o.gpu == new.gpu and o.id != new.id]
    per: dict[int, list[dict]] = {}
    for a, z, active in _spans(others, new.start, new.end):
        before = allocate(active, total_mib)
        after = allocate(active + [new], total_mib)
        for o in active:
            lost = before[o.id] - after[o.id]
            if lost > 0:
                _merge(per.setdefault(o.id, []), {"id": o.id, "user": o.user, "gpu": o.gpu,
                                                  "vram_mib": lost, "start": a, "end": z},
                       ("vram_mib",))
    return sorted((s for segs in per.values() for s in segs),
                  key=lambda s: (s["start"], s["id"]))


def _check_capacity(overlapping: list[Booking], gpu: int, vram_mib: int, start: datetime,
                    end: datetime, card_mib: int, priority: str = "lendable") -> None:
    """Booked VRAM only rises at a booking's start, so checking the new window's start
    and every overlapping start inside it covers the worst instant. A lendable booking
    must fit beside every booking; an important one only beside the important ones."""
    if priority == "important":
        overlapping = [b for b in overlapping if b.priority == "important"]
    instants = sorted({start} | {b.start for b in overlapping if start < b.start < end})
    for t in instants:
        active = [b for b in overlapping if b.active_at(t)]
        booked = sum(b.vram_mib for b in active)
        if booked + vram_mib > card_mib:
            held = [b for b in active if b.kind == "quick"]
            if held:
                # The holder name was validated when the quick booking was made. The end is
                # shown as local Europe/Berlin wall-clock HH:MM, like the 09:00 reset.
                h = held[0]
                until = h.end.astimezone(ZoneInfo(QUICK_TZ)).strftime("%H:%M")
                raise ClaimError(409, f"GPU {gpu} is held by {h.user} until {until} "
                                      "(quick booking) — set its holder to free first")
            t2 = min(min(b.end for b in active), end)
            free = max(card_mib - booked, 0) / MIB_PER_GIB
            what = "not booked as important" if priority == "important" else "unbooked"
            raise ClaimError(409, f"GPU {gpu} has only {free:.1f} GiB {what} between "
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
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _migrate(self) -> None:
        """Version 1 -> 2 adds claims.kind; existing rows become 'calendar'. 2 -> 3 adds
        claims.priority; existing rows become 'important', since they were made when every
        booking blocked others. One transaction, and each column is checked first, so a
        half-done or repeated run is harmless."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
                if "kind" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN kind TEXT NOT NULL "
                                 "DEFAULT 'calendar'")
                if "priority" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN priority TEXT NOT NULL "
                                 "DEFAULT 'important'")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def create(self, *, user: str, gpu: int, vram_mib: int, start: datetime, end: datetime,
               note: str | None, ip: str, now: datetime,
               card_mib: int = DEFAULT_CARD_MIB, priority: str = "lendable",
               grace: timedelta = DEFAULT_GRACE) -> Created:
        """Book, or raise ClaimError. An important booking that would squeeze a lendable
        booking already under way starts no earlier than now + grace; the start it asked
        for is then returned as adjusted_start."""
        if priority not in PRIORITIES:
            raise ClaimError(400, "priority must be lendable or important")
        start, end, now = _utc(start), _utc(end), _utc(now)
        note = _validate(self.users, user, gpu, vram_mib, start, end, note, now, card_mib)
        asked = start
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                overlapping = [_row(r) for r in conn.execute(
                    f"SELECT {_COLUMNS} FROM claims WHERE gpu = ? AND cancelled_at IS NULL "
                    "AND start < ? AND end > ?", (gpu, end.isoformat(), start.isoformat()))]
                _check_capacity(overlapping, gpu, vram_mib, start, end, card_mib, priority)

                def draft(s: datetime) -> Booking:
                    return Booking(id=0, user=user, gpu=gpu, vram_mib=vram_mib, start=s,
                                   end=end, note=note, created_at=now, created_ip=ip,
                                   priority=priority)
                takes = takes_of(draft(start), overlapping, card_mib) \
                    if priority == "important" else []
                running = {b.id for b in overlapping if b.start <= now}
                if any(t["id"] in running and t["start"] < now + grace for t in takes):
                    start = now + grace
                    if end <= start:
                        raise ClaimError(409, "this takes from a lendable booking that is "
                                              "already running, so it must last beyond "
                                              f"{int(grace.total_seconds() // 60)} minutes "
                                              "from now")
                    takes = takes_of(draft(start), overlapping, card_mib)
                cur = conn.execute(
                    "INSERT INTO claims (user, gpu, vram_mib, start, end, note, created_at, "
                    "created_ip, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user, gpu, vram_mib, start.isoformat(), end.isoformat(), note,
                     now.isoformat(), ip, priority))
                claim_id = cur.lastrowid
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        booking = Booking(id=claim_id, user=user, gpu=gpu, vram_mib=vram_mib, start=start,
                          end=end, note=note, created_at=now, created_ip=ip, priority=priority)
        return Created(booking, takes, asked if start != asked else None)

    def quick(self, *, user: str | None, gpu: int, ip: str, now: datetime,
              card_mib: int = DEFAULT_CARD_MIB) -> Booking | None:
        """Set (user) or clear (None) a GPU's quick holder: the whole card from now until the
        next 09:00 Berlin, cut short by the first future calendar booking on that GPU. Refused
        while a calendar booking is active; calendar bookings are never touched."""
        now = _utc(now)
        _validate_gpu(gpu)
        if user is not None:
            _validate_user(self.users, user)
        ts = now.isoformat()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM claims WHERE gpu = ? AND cancelled_at IS NULL "
                                "AND kind = 'calendar' AND start <= ? AND end > ? LIMIT 1",
                                (gpu, ts, ts)).fetchone():
                    raise ClaimError(409, f"GPU {gpu} has calendar bookings — use the calendar")
                end = next_reset(now)
                if user is not None:
                    row = conn.execute(
                        "SELECT MIN(start) FROM claims WHERE gpu = ? AND cancelled_at IS NULL "
                        "AND kind = 'calendar' AND start > ? AND start < ?",
                        (gpu, ts, end.isoformat())).fetchone()
                    if row[0] is not None:
                        end = datetime.fromisoformat(row[0])
                        if end - now < QUICK_MIN:
                            raise ClaimError(409, f"GPU {gpu} is booked from "
                                                  f"{end.strftime(_TIME_FMT)} — use the calendar")
                conn.execute("UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                             "WHERE gpu = ? AND kind = 'quick' AND cancelled_at IS NULL "
                             "AND start <= ? AND end > ?", (ts, ip, gpu, ts, ts))
                claim_id = None
                if user is not None:
                    claim_id = conn.execute(
                        "INSERT INTO claims (user, gpu, vram_mib, start, end, note, created_at, "
                        "created_ip, kind, priority) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'quick', 'lendable')",
                        (user, gpu, card_mib, ts, end.isoformat(), QUICK_NOTE, ts,
                         ip)).lastrowid
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if claim_id is None:
            return None
        return Booking(id=claim_id, user=user, gpu=gpu, vram_mib=card_mib, start=now, end=end,
                       note=QUICK_NOTE, created_at=now, created_ip=ip, kind="quick",
                       priority="lendable")

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


def _read_ro(path: Path | str, where: str, params: tuple) -> list[Booking]:
    """SELECT booking rows matching `where` with mode=ro. Fail-open: a missing, corrupt, locked
    or malformed file means no bookings, and the error is printed by exception class only.
    A file the web process has not migrated yet reads as calendar (v1) and important."""
    try:
        path = Path(path)
        if not path.exists():
            return []
        # as_uri() percent-encodes '?', '#' and '%', so they cannot leak into the query string.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
            select = (_COLUMNS if "priority" in cols else
                      _COLUMNS_V2_AS_V3 if "kind" in cols else _COLUMNS_V1_AS_V3)
            rows = conn.execute(f"SELECT {select} FROM claims {where}", params).fetchall()
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
    return _read_ro(path, "WHERE cancelled_at IS NULL AND start <= ? AND end > ? "
                    "ORDER BY gpu, start, id", (ts, ts))


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
    return _read_ro(path, "WHERE end >= ? AND start <= ? ORDER BY start, id", (lo, hi))
