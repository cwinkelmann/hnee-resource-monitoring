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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

DEFAULT_CLAIMS = Path.home() / ".local/state/resourcemonitor/claims.sqlite"
MIB_PER_GIB = 1024
DEFAULT_CARD_MIB = 81559
MAX_DAYS = 14
PAST_SLACK = timedelta(minutes=5)
NOTE_MAX = 120
GPU_COUNT = 8
SCHEMA_VERSION = 4
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
  priority     TEXT    NOT NULL DEFAULT 'important',
  auto_advance INTEGER NOT NULL DEFAULT 0,
  group_id     TEXT,
  notice_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS claims_gpu_window ON claims(gpu, start, end);
"""

_COLUMNS_V1 = ("id, user, gpu, vram_mib, start, end, note, created_at, created_ip, "
               "cancelled_at, cancelled_ip")
_COLUMNS = _COLUMNS_V1 + ", kind, priority, auto_advance, group_id, notice_seconds"
# Older files read before the web process migrated them: v1 has no kind (all calendar),
# v2 no priority, v3 none of the assignment columns. Rows from before priorities existed
# blocked everyone, so they read as important; rows from before assignment never move.
_COLUMNS_V3_AS_V4 = _COLUMNS_V1 + ", kind, priority, 0, NULL, NULL"
_COLUMNS_V2_AS_V4 = _COLUMNS_V1 + ", kind, 'important', 0, NULL, NULL"
_COLUMNS_V1_AS_V4 = _COLUMNS_V1 + ", 'calendar', 'important', 0, NULL, NULL"


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
    auto_advance: bool = False              # may this booking move earlier by itself?
    group_id: str | None = None             # members of one atomic group share it
    notice_seconds: int | None = None       # warning this owner needs; None: nobody listens

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
            "auto_advance": self.auto_advance, "group_id": self.group_id,
            "notice_seconds": self.notice_seconds,
        }


@dataclass(frozen=True)
class Created:
    """New bookings (one row, or every member of a group), the lendable bookings they take
    from, the start that was asked for if grace moved it, whether the server had to assign
    a later slot, and the bookings revoked to make room."""
    bookings: list[Booking]
    takes: list[dict]
    adjusted_start: datetime | None = None
    assigned_later: bool = False
    revoked: list[dict] = field(default_factory=list)

    @property
    def booking(self) -> Booking:
        """The first row. Every caller that predates group bookings uses this."""
        return self.bookings[0]


@dataclass(frozen=True)
class Released:
    """A released booking and any assigned slots that moved earlier because of it."""
    booking: Booking
    advanced: list[dict] = field(default_factory=list)


class ClaimError(Exception):
    def __init__(self, status: int, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        # Machine-readable detail for a 409: the earliest slot that would fit and how much
        # is free now, so "book later or book less" is in the answer instead of guesswork.
        self.extra = extra or {}


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
                   priority=r[12], auto_advance=bool(r[13]), group_id=r[14],
                   notice_seconds=r[15])


def _validate_user(users: UserDirectory, user: str) -> None:
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ClaimError(400, "invalid user name")
    if not users.known(user):
        raise ClaimError(400, f"unknown user '{user}'")


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _validate_gpu(gpu: int) -> None:
    if not 0 <= gpu < GPU_COUNT:
        raise ClaimError(400, "GPU must be 0–7")


def _validate_window(start: datetime, end: datetime, now: datetime) -> None:
    """The rules a window must satisfy. Split out of _validate because a window the server
    assigned is checked after it is chosen, not before."""
    if end <= start:
        raise ClaimError(400, "end must be after start")
    if start < now - PAST_SLACK:
        raise ClaimError(400, "start lies in the past")
    if end - start > timedelta(days=MAX_DAYS):
        raise ClaimError(400, f"a booking may last at most {MAX_DAYS} days")


def _validate_note(note: str | None) -> str | None:
    if note is None:
        return None
    if len(note) > NOTE_MAX:
        raise ClaimError(400, "note too long")
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in note):
        raise ClaimError(400, "invalid note")
    return note if note.strip() else None


def _validate(users: UserDirectory, user: str, gpu: int, vram_mib: int, start: datetime,
              end: datetime, note: str | None, now: datetime, card_mib: int) -> str | None:
    """Raise ClaimError(400) on the first broken rule; return the note to store."""
    _validate_user(users, user)
    _validate_gpu(gpu)
    if vram_mib < MIB_PER_GIB or vram_mib > card_mib:
        raise ClaimError(400, f"VRAM must be between 1 and {card_mib // MIB_PER_GIB} GiB")
    _validate_window(start, end, now)
    return _validate_note(note)


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


def _all_cards() -> dict[int, int]:
    """Every GPU at the default card size: the fallback when a caller has no live totals."""
    return {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}


def _free_now(rows: list[Booking], cards: dict[int, int], gpu: int | None,
              now: datetime) -> int:
    """The most MiB free on one card at `now` -- what "book less" would have to fit into.
    With gpu=None that is the best card, which is the number a floating booking cares about."""
    from resourcemonitor.slots import booked_at
    want = cards if gpu is None else {gpu: cards.get(gpu, DEFAULT_CARD_MIB)}
    return max((max(mib - booked_at(rows, g, now), 0) for g, mib in want.items()), default=0)


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
        booking blocked others. 3 -> 4 adds the assignment columns: auto_advance (may this
        booking move earlier by itself), group_id (members of one atomic group) and
        notice_seconds (the warning its owner needs before losing the card); existing rows
        get 0 and NULL, which is exactly how a booking made in the browser behaves.
        One transaction, and each column is checked first, so a half-done or repeated run
        is harmless."""
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
                if "auto_advance" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN auto_advance INTEGER "
                                 "NOT NULL DEFAULT 0")
                if "group_id" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN group_id TEXT")
                if "notice_seconds" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN notice_seconds INTEGER")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def create(self, *, user: str, gpu: int | None, vram_mib: int,
               start: datetime | None, end: datetime | None, note: str | None, ip: str,
               now: datetime, card_mib: int = DEFAULT_CARD_MIB,
               priority: str = "lendable", grace: timedelta = DEFAULT_GRACE,
               duration: timedelta | None = None, auto_advance: bool = False,
               not_later_than: datetime | None = None, count: int = 1,
               cards: dict[int, int] | None = None,
               max_pending: int | None = None, notice_seconds: int | None = None,
               watching: Callable[[int], bool] = lambda _id: False) -> Created:
        """Book, or raise ClaimError. With start=None the server assigns the earliest slot
        that fits and the booking never fails for lack of capacity; with an explicit start
        the old behaviour is kept exactly, 409 included. An important booking that would
        squeeze a lendable booking already under way starts no earlier than now + grace.

        Give exactly one of end and duration. gpu=None lets the server pick the card.
        count > 1 books that many cards atomically: one group_id, one shared start.

        notice_seconds is the warning *this* booking's owner promises to need before giving
        up its card; `watching` answers whether a given booking id has a live watcher, and
        is consulted about this booking's victims.
        """
        from resourcemonitor.slots import earliest_fit, earliest_fit_group, fits
        if priority not in PRIORITIES:
            raise ClaimError(400, "priority must be lendable or important")
        if (end is None) == (duration is None):
            raise ClaimError(400, "give exactly one of end and duration")
        if notice_seconds is not None and (not isinstance(notice_seconds, int)
                                           or isinstance(notice_seconds, bool)
                                           or notice_seconds < 0):
            raise ClaimError(400, "notice_seconds must be 0 or more")
        if not _is_count(count):
            raise ClaimError(400, "count must be a whole number of GPUs, 1 or more")
        if count > GPU_COUNT:
            raise ClaimError(400, f"count must be at most {GPU_COUNT}")
        if count > 1 and (gpu is not None or start is not None):
            raise ClaimError(400, "a group booking cannot name a GPU or a start")
        now = _utc(now)
        if duration is None:
            duration = _utc(end) - _utc(start if start is not None else now)
        if duration <= timedelta(0):
            raise ClaimError(400, "end must be after start")
        if duration > timedelta(days=MAX_DAYS):
            raise ClaimError(400, f"a booking may last at most {MAX_DAYS} days")
        cards = dict(cards) if cards else {g: card_mib for g in range(GPU_COUNT)}
        _validate_user(self.users, user)
        if gpu is not None:
            _validate_gpu(gpu)
            card_mib = cards.get(gpu, card_mib)
        if vram_mib < MIB_PER_GIB or vram_mib > max(cards.values()):
            raise ClaimError(400, "VRAM must be between 1 and "
                                  f"{max(cards.values()) // MIB_PER_GIB} GiB")
        note = _validate_note(note)
        asked = None if start is None else _utc(start)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if max_pending is not None:
                    # Counted in rows, so a four-card group costs four. Without this, a
                    # 13,000-job pipeline books the whole calendar and a colleague who
                    # needs 80 GiB is offered a slot a fortnight out.
                    held = conn.execute(
                        "SELECT COUNT(*) FROM claims WHERE user = ? AND cancelled_at IS "
                        "NULL AND start > ?", (user, now.isoformat())).fetchone()[0]
                    if held + count > max_pending:
                        raise ClaimError(409,
                                         f"you already hold {held} bookings that have not "
                                         f"started; the limit is {max_pending}",
                                         {"max_pending_per_user": max_pending,
                                          "pending": held})
                if start is None:
                    rows = [_row(r) for r in conn.execute(
                        f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL "
                        "AND end > ?", (now.isoformat(),))]
                    # A lendable booking must fit beside every booking; an important one
                    # only beside the important ones, exactly as _check_capacity has it --
                    # otherwise "earliest" would queue an important booking behind the
                    # lendable work it is entitled to take.
                    fit_rows = rows if priority != "important" else \
                        [b for b in rows if b.priority == "important"]
                    hit = earliest_fit_group(fit_rows, count, vram_mib, duration, cards,
                                             now, gpus=None if gpu is None else [gpu])
                    if hit is None:
                        raise ClaimError(409, f"no slot fits within the next {MAX_DAYS} days",
                                         {"earliest": None,
                                          "free_now": _free_now(fit_rows, cards, gpu, now)})
                    start, group_gpus = hit
                    if not_later_than is not None and start > _utc(not_later_than):
                        raise ClaimError(409, "the earliest slot is later than asked",
                                         {"earliest": start.isoformat(),
                                          "free_now": _free_now(fit_rows, cards,
                                                                group_gpus[0], now)})
                    gpu, card_mib = group_gpus[0], cards[group_gpus[0]]
                    end = start + duration
                    assigned_later = start > now
                    overlapping = [b for b in rows if b.gpu == gpu
                                   and b.start < end and b.end > start]
                    snapshot = rows
                else:
                    start = asked
                    end = start + duration if end is None else _utc(end)
                    _validate_window(start, end, now)
                    if gpu is None:
                        # "Any card" has to mean the same thing here as it does on the
                        # assigned path, or this reaches the INSERT with a NULL gpu and
                        # answers 500. The window is fixed, so only the card is chosen.
                        rows = [_row(r) for r in conn.execute(
                            f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL "
                            "AND start < ? AND end > ?",
                            (end.isoformat(), start.isoformat()))]
                        fit_rows = rows if priority != "important" else \
                            [b for b in rows if b.priority == "important"]
                        pick = next((g for g in sorted(cards)
                                     if fits(fit_rows, g, vram_mib, start,
                                             end - start, cards[g])), None)
                        if pick is None:
                            raise ClaimError(409, "no card has room for that window",
                                             {"earliest": None,
                                              "free_now": _free_now(fit_rows, cards,
                                                                    None, now)})
                        gpu, card_mib = pick, cards[pick]
                    group_gpus = [gpu]
                    assigned_later = False
                    overlapping = [_row(r) for r in conn.execute(
                        f"SELECT {_COLUMNS} FROM claims WHERE gpu = ? "
                        "AND cancelled_at IS NULL AND start < ? AND end > ?",
                        (gpu, end.isoformat(), start.isoformat()))]
                    snapshot = overlapping
                    try:
                        _check_capacity(overlapping, gpu, vram_mib, start, end, card_mib,
                                        priority)
                    except ClaimError as e:
                        if e.status != 409:
                            raise
                        fit = earliest_fit(overlapping, gpu, vram_mib, duration, card_mib,
                                           now)
                        raise ClaimError(409, e.detail, {
                            "earliest": None if fit is None else fit.isoformat(),
                            "free_now": _free_now(overlapping, cards, gpu, now)}) from None

                def draft(s: datetime) -> Booking:
                    return Booking(id=0, user=user, gpu=gpu, vram_mib=vram_mib, start=s,
                                   end=s + duration if assigned_later or asked is None
                                   else end, note=note, created_at=now, created_ip=ip,
                                   priority=priority)
                takes = takes_of(draft(start), overlapping, card_mib) \
                    if priority == "important" else []
                running = {b.id: b for b in overlapping if b.start <= now}
                victims = {t["id"] for t in takes if t["id"] in running}

                def notice_of(v: Booking) -> timedelta:
                    """A victim that is being listened to may shorten the wait to what it
                    declared; it can never lengthen it past the policy grace, or a job
                    could make itself unpreemptable by asking for a day's notice. A victim
                    whose watcher has gone is treated as unwatched: a stale promise must
                    not fake a response nobody is there to give."""
                    if v.notice_seconds is None or not watching(v.id):
                        return grace
                    return min(timedelta(seconds=v.notice_seconds), grace)

                notice = max((notice_of(running[i]) for i in victims),
                             default=timedelta(0))
                if notice > timedelta(0) and any(t["id"] in victims
                                                 and t["start"] < now + notice
                                                 for t in takes):
                    start = now + notice         # rounded up to the whole minute
                    if start.second or start.microsecond:
                        start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
                    if asked is None:
                        # An assigned window keeps the duration it asked for: the job wanted
                        # an hour, not whatever is left of one. The whole window has moved,
                        # so the fit must be computed again -- `overlapping` was read for
                        # the window earliest_fit first offered, and a row that overlaps
                        # only the new one would be invisible to a re-check against it.
                        again = earliest_fit_group(fit_rows, count, vram_mib, duration,
                                                   cards, start,
                                                   gpus=None if group_gpus is None
                                                   else group_gpus)
                        if again is None:
                            raise ClaimError(409, "no slot fits after the notice its "
                                                  f"victims are owed ({MAX_DAYS} days "
                                                  "checked)",
                                             {"earliest": None,
                                              "free_now": _free_now(fit_rows, cards, gpu,
                                                                    now)})
                        start, group_gpus = again
                        gpu, card_mib = group_gpus[0], cards[group_gpus[0]]
                        end = start + duration
                        overlapping = [b for b in rows if b.gpu == gpu
                                       and b.start < end and b.end > start]
                        assigned_later = start > now
                    elif end <= start:
                        raise ClaimError(409, "this takes from a lendable booking that is "
                                              "already running, so it must last beyond "
                                              f"{int(notice.total_seconds() // 60)} minutes "
                                              "from now")
                    takes = takes_of(draft(start), overlapping, card_mib)
                # One snapshot, one BEGIN IMMEDIATE: that is what makes a group atomic --
                # no second writer can take the third card between members two and four.
                group_id = uuid.uuid4().hex if count > 1 else None
                made: list[Booking] = []
                for g in group_gpus:
                    cur = conn.execute(
                        "INSERT INTO claims (user, gpu, vram_mib, start, end, note, "
                        "created_at, created_ip, priority, auto_advance, group_id, "
                        "notice_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (user, g, vram_mib, start.isoformat(), end.isoformat(), note,
                         now.isoformat(), ip, priority, int(auto_advance), group_id,
                         notice_seconds))
                    made.append(Booking(id=cur.lastrowid, user=user, gpu=g,
                                        vram_mib=vram_mib, start=start, end=end, note=note,
                                        created_at=now, created_ip=ip, priority=priority,
                                        auto_advance=auto_advance, group_id=group_id,
                                        notice_seconds=notice_seconds))
                revoked: list[dict] = []
                for m in made:
                    revoked += self._revoke(
                        conn, booking_row=m, card_mib=cards.get(m.gpu, card_mib),
                        overlapping=[b for b in snapshot if b.gpu == m.gpu
                                     and b.id != m.id and b.start < m.end
                                     and b.end > m.start])
                if any(r["truncated"] for r in revoked):
                    # The freed tail is capacity like any other: whoever is waiting for it
                    # moves up, inside this same transaction.
                    self._advance(conn, now, cards)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return Created(made, takes,
                       asked if asked is not None and start != asked else None,
                       assigned_later, revoked)

    def _advance(self, conn, now: datetime, cards: dict[int, int]) -> list[dict]:
        """Move every advanceable booking to the earliest slot that now fits, re-reading
        the timeline after each move so advances cascade. Called only from inside the
        transaction of a write that freed capacity -- time passing can never make a slot
        advanceable, because earliest_fit already priced in every known end."""
        from resourcemonitor.slots import advance_order, earliest_fit, earliest_fit_group
        moved: list[dict] = []

        def timeline() -> list[Booking]:
            return [_row(r) for r in conn.execute(
                f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL AND end > ?",
                (now.isoformat(),))]

        rows = timeline()
        pending = [b for b in rows if b.auto_advance and b.start > now]
        seen_groups: set[str] = set()
        for b in advance_order(pending):
            if b.group_id is not None and b.group_id in seen_groups:
                continue
            members = [m for m in rows if m.group_id == b.group_id] \
                if b.group_id is not None else [b]
            duration = b.end - b.start
            others = [r for r in rows if r.id not in {m.id for m in members}]
            if b.group_id is None:
                hit = earliest_fit(others, b.gpu, b.vram_mib, duration,
                                   cards.get(b.gpu, DEFAULT_CARD_MIB), now)
                new_start, new_gpus = (hit, [b.gpu]) if hit is not None else (None, [])
            else:
                seen_groups.add(b.group_id)
                got = earliest_fit_group(others, len(members), b.vram_mib, duration,
                                         cards, now)
                new_start, new_gpus = got if got is not None else (None, [])
            if new_start is None or new_start >= b.start:
                continue
            for m, g in zip(members, new_gpus):
                conn.execute("UPDATE claims SET start = ?, end = ?, gpu = ? WHERE id = ?",
                             (new_start.isoformat(), (new_start + duration).isoformat(),
                              g, m.id))
                moved.append({"id": m.id, "user": m.user, "gpu": g,
                              "old_start": m.start.isoformat(),
                              "new_start": new_start.isoformat()})
            rows = timeline()
        return moved

    def _revoke(self, conn, *, booking_row: Booking, overlapping: list[Booking],
                card_mib: int) -> list[dict]:
        """Write down what this important booking takes. allocate() already decided it:
        important bookings get their full VRAM and lendable ones split what is left,
        oldest id first, so the newest lendable booking is the one that starves. A victim
        left with nothing has its end truncated to the instant it loses the card -- the
        window it really used stays on the calendar, exactly as an early release. A victim
        merely shrunk keeps its row and is only told.

        "Left with nothing" means left with less than MIB_PER_GIB: 79 GiB taken from a
        79 GiB booking on an 81559 MiB card leaves a 663 MiB crumb, not a clean zero, and
        a share below the smallest bookable amount cannot run anything -- leaving that row
        on the calendar would block the card for the rest of its window for no one.

        Only a booking active at that instant is ever touched, so end can never fall below
        start, and `end > ?` makes a repeat harmless."""
        if booking_row.priority != "important":
            return []
        at = booking_row.start
        live = [b for b in overlapping if b.cancelled_at is None and b.active_at(at)]
        after = allocate(live + [booking_row], card_mib)
        out: list[dict] = []
        for v in sorted((b for b in live if b.priority != "important"), key=lambda b: b.id):
            effective = after.get(v.id, v.vram_mib)
            if effective >= v.vram_mib:
                continue
            truncated = cancelled = False
            if effective < MIB_PER_GIB:
                if v.start >= at:
                    # It never ran: truncating would leave end == start, a row
                    # _validate_window would reject and the calendar cannot draw.
                    cur = conn.execute(
                        "UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                        "WHERE id = ? AND cancelled_at IS NULL",
                        (at.isoformat(), booking_row.created_ip, v.id))
                    cancelled = truncated = cur.rowcount > 0
                else:
                    cur = conn.execute("UPDATE claims SET end = ? WHERE id = ? AND end > ?",
                                       (at.isoformat(), v.id, at.isoformat()))
                    truncated = cur.rowcount > 0
            out.append({"id": v.id, "user": v.user, "gpu": v.gpu,
                        "effective_mib": effective, "lost_mib": v.vram_mib - effective,
                        "at": at.isoformat(), "by": [booking_row.user],
                        "truncated": truncated, "cancelled": cancelled})
        return out

    def release(self, claim_id: int, *, end: datetime | None, ip: str, now: datetime,
                cards: dict[int, int] | None = None) -> Released:
        """Give a card back honestly. A booking under way has its end moved, so the window
        it really used stays on the calendar and in the energy ledger; a booking that has
        not started is cancelled instead, because end < start is a nonsense row. Moving the
        end *later* is an extension and re-checks capacity, so it can lose to an assigned
        slot. Releasing a booking that is already over is a no-op."""
        now = _utc(now)
        new_end = now if end is None else _utc(end)
        # The row is read again inside the transaction, so this is an early exit, not a race.
        b = self.get(claim_id)
        if b is None:
            raise ClaimError(404, "no such booking")
        if b.cancelled_at is not None:
            raise ClaimError(409, "already cancelled")
        if b.start > now:
            if new_end > b.start:
                # Giving back a booking that has not started means cancelling it. Asking
                # for a *later* end is an extension, and routing that to cancel() would
                # destroy the booking the caller was trying to lengthen -- while answering
                # 200.
                raise ClaimError(409, "this booking has not started: release it with no "
                                      "end to cancel it, or book the later window")
            booking, moved = self._cancel_inner(claim_id, ip=ip, now=now, cards=cards)
            return Released(booking, moved)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(f"SELECT {_COLUMNS} FROM claims WHERE id = ?",
                                   (claim_id,)).fetchone()
                if row is None:
                    raise ClaimError(404, "no such booking")
                b = _row(row)
                if b.cancelled_at is not None:
                    raise ClaimError(409, "already cancelled")
                if b.end <= now:
                    conn.execute("COMMIT")
                    return Released(b)          # already over: nothing left to give back
                if new_end < b.start:
                    raise ClaimError(400, "end must be after start")
                if new_end > b.end:
                    overlapping = [_row(r) for r in conn.execute(
                        f"SELECT {_COLUMNS} FROM claims WHERE gpu = ? "
                        "AND cancelled_at IS NULL AND id != ? AND start < ? AND end > ?",
                        (b.gpu, b.id, new_end.isoformat(), b.end.isoformat()))]
                    card = (cards or {}).get(b.gpu, DEFAULT_CARD_MIB)
                    _check_capacity(overlapping, b.gpu, b.vram_mib, b.end, new_end, card,
                                    b.priority)
                conn.execute("UPDATE claims SET end = ? WHERE id = ?",
                             (new_end.isoformat(), claim_id))
                out = _row(conn.execute(f"SELECT {_COLUMNS} FROM claims WHERE id = ?",
                                        (claim_id,)).fetchone())
                moved = self._advance(conn, now, cards or _all_cards())
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return Released(out, moved)

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

    def cancel(self, claim_id: int, *, ip: str, now: datetime,
               cards: dict[int, int] | None = None) -> Booking:
        """See _cancel_inner. The cancel route reports no moves, so it drops them."""
        return self._cancel_inner(claim_id, ip=ip, now=now, cards=cards)[0]

    def _cancel_inner(self, claim_id: int, *, ip: str, now: datetime,
                      cards: dict[int, int] | None = None) -> tuple[Booking, list[dict]]:
        """Idempotent: cancelling an already-cancelled booking changes nothing.
        Cancelling a not-yet-started member of a group cancels the whole set -- a group
        exists so its cards are held together, and a partial group would sit on idle cards
        waiting for a member that is already gone."""
        now = _utc(now)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                head = conn.execute("SELECT group_id, start FROM claims WHERE id = ?",
                                    (claim_id,)).fetchone()
                if head is not None and head[0] is not None and _ts(head[1]) > now:
                    conn.execute("UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                                 "WHERE group_id = ? AND cancelled_at IS NULL "
                                 "AND start > ?",
                                 (now.isoformat(), ip, head[0], now.isoformat()))
                conn.execute("UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                                "WHERE id = ? AND cancelled_at IS NULL",
                                (now.isoformat(), ip, claim_id))
                row = conn.execute(f"SELECT {_COLUMNS} FROM claims WHERE id = ?",
                                      (claim_id,)).fetchone()
                moved = []
                if row is not None:
                    moved = self._advance(conn, now, cards or _all_cards())
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if row is None:
            raise ClaimError(404, "no such booking")
        return _row(row), moved

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
            select = (_COLUMNS if "notice_seconds" in cols else
                      _COLUMNS_V3_AS_V4 if "priority" in cols else
                      _COLUMNS_V2_AS_V4 if "kind" in cols else _COLUMNS_V1_AS_V4)
            rows = conn.execute(f"SELECT {select} FROM claims {where}", params).fetchall()
        finally:
            conn.close()
        return [_row(r) for r in rows]
    except Exception as e:
        print(f"claims unavailable: {type(e).__name__}")
        return []


def get_ro(path: Path | str, claim_id: int) -> Booking | None:
    """One booking, read with mode=ro (fail-open, see _read_ro). A GET never opens the
    bookings file for writing."""
    if not isinstance(claim_id, int) or isinstance(claim_id, bool):
        return None
    rows = _read_ro(path, "WHERE id = ? LIMIT 1", (claim_id,))
    return rows[0] if rows else None


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
