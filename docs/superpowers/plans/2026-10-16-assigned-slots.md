# Assigned Slots and Revocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A booking never fails for lack of capacity — it is assigned the earliest slot that fits, moves earlier when someone releases early, and a `lendable` booking can be revoked by an `important` one after a notice the victim itself declared, delivered over a long-poll the job dials out on.

**Architecture:** One new pure module (`resourcemonitor/slots.py`) answers *when does this fit*, with no SQL and no clock. `claims.py` keeps its role as the only writer of `claims.sqlite`: schema v3 → v4 adds three columns, `create()` learns to choose a start, and `release()` truncates instead of cancelling. Advancing and revocation happen **inside the transaction of the write that caused them** — there is no timer, thread or cron anywhere. One new in-memory module (`resourcemonitor/watchers.py`) holds parked long-polls; `web.py` gains three routes. The repo's hard rule survives untouched: the server writes rows, never processes.

**Tech Stack:** Python 3.10+, standard library only (`sqlite3`, `http.server`, `threading`, `tomllib`). pytest for tests. No new dependency, in this repo or on the hosts.

**Spec:** `docs/superpowers/specs/2026-10-15-assigned-slots-design.md` — read it first; this plan argues from it and does not repeat its reasoning.

## Global Constraints

Copied from `CLAUDE.md` and the spec. Every task's requirements include this section.

- **Stdlib only.** No third-party package, ever, not even in tests beyond pytest.
- **The server never kills, signals or reprioritises a process.** Revocation writes a row and notifies a client. A change that writes to another user's process does not belong in this repo.
- **`probe` and `notify` are never imported by `web.py` or `claims.py`.**
- **`claims.py` is the only module that writes `claims.sqlite`.** `history.sqlite` stays `mode=ro` to the web process.
- **Rows are never deleted.** Cancelling sets `cancelled_at`; releasing moves `end`. The table is the audit trail and the energy ledger's source.
- **Schema changes are additive and migrate forward only**, following `_migrate()`'s existing shape: `PRAGMA user_version`, one transaction, each column checked before it is added, so a repeated or half-finished run is harmless.
- **Dry-run is the default; `--post` is opt-in.** Nothing in this plan touches the notify path.
- **The Slack webhook never appears in a log, a repr or a traceback.** Nothing in this plan reads it.
- **Never hold the `Watchers` lock while taking the `ClaimsStore` lock.** Notifications are collected inside the DB transaction and delivered *after* `COMMIT`. This one ordering rule is what keeps the long-poll from deadlocking a booking write.
- **The write rate limiter is `_RateLimiter(limit=30)` on this branch.** PR #7 raises it to 1000/h and is not merged; do **not** touch `_WRITES` in this plan or that history will be duplicated. The acceptance script in Task 16 makes 8 writes, so it passes under either value.
- **`GPU_COUNT = 8`, `MAX_DAYS = 14`, `DEFAULT_CARD_MIB = 81559`, `MIB_PER_GIB = 1024`** stay as they are. t14 has one card of 16376 MiB, which the existing `_card_mib()` already discovers from history.

## Review Focus

Five inputs the spec implies and no task's happy path exercises. Each one's test is written into the task that owns the code, named in brackets.

1. **A victim declaring `notice_seconds` larger than `grace_minutes` would become unpreemptable.** A job could veto revocation for a day by asking for a day's notice. Notice may only ever *shorten* the wait: `effective = min(declared, grace_minutes)`. [Task 12]
2. **`duration_minutes` of 0, negative, non-integer, or beyond `MAX_DAYS`** must answer `400 invalid` before any capacity scan — not assign a zero-length slot or a fortnight. [Task 8]
3. **A watch on a booking that is cancelled, already over, or unknown** must answer at once (`409`, `409`, `404`) and never park a connection that nothing will ever wake. [Task 14]
4. **Two important bookings revoking the same victim in the same second** must leave one truncation, never `end < start`, and never a second notification claiming VRAM that was already gone. [Task 13]
5. **A parked long-poll must not delay `server_close()`.** A held request that outlives shutdown would hang the service restart the user performs by hand on every deploy. [Task 14]

---

## File Structure

**Created:**
- `resourcemonitor/slots.py` — pure capacity arithmetic: when does a shape fit, and in what order do pending bookings get to move. No SQL, no clock, no I/O. ~110 lines.
- `resourcemonitor/watchers.py` — in-memory registry of parked long-polls, one `threading.Condition`. Knows nothing about HTTP or bookings beyond an id. ~70 lines.
- `tests/test_slots.py` — table-driven, no DB, no GPU, no server.
- `tests/test_watchers.py` — threads, no HTTP.
- `tests/test_claims_assign.py` — assignment and groups through `ClaimsStore`.
- `tests/test_claims_release.py` — truncate, extend, cancel fallback.
- `tests/test_claims_advance.py` — advancing inside a releasing transaction.
- `tests/test_claims_revoke.py` — notice, truncation of a starved victim, concurrency.
- `tests/test_web_events.py` — the long-poll route end to end.
- `deploy/acceptance-slots.py` — the t14 acceptance run: a stdlib script that plays two jobs against a live `serve`. Stands in for the SDK, which lives in another repo.

**Modified:**
- `resourcemonitor/claims.py` — `SCHEMA_VERSION`, three columns, `Booking`/`Created`/`ClaimError` fields, `create()` assignment and revocation, new `release()`, `_advance()`.
- `resourcemonitor/web.py` — new POST fields, `POST /api/claims/{id}/release`, `GET /api/capacity`, `GET /api/claims/{id}/events`, a `Watchers` on the server object.
- `resourcemonitor/policy.py` — `max_pending_per_user`, `max_watchers`.
- `deploy/policy.example.toml` — the two new keys, documented.
- `resourcemonitor/web/app.js`, `app.css` — assigned-slot and revoked styling.
- `tests/claims_fixture.py` — a `cards()` helper and `book(auto_advance=…)`.
- `tests/test_claims.py`, `tests/test_claims_priority.py`, `tests/test_web_claims.py` — kept passing as signatures grow.
- `README.md`, `.claude/skills/deploy-resourcemonitor/SKILL.md` — the two new policy keys and the acceptance run.

**Shippable slices.** After Task 10 the repo has working assigned slots and can be deployed on its own. Tasks 11–14 add revocation. Task 16 is the t14 acceptance run and needs both.

**Out of scope, deliberately:** the `hrm` SDK. It lives in `~/work/hnee-resource-monitoring-sdk` on branch `feat/sdk` and gets its own plan in its own repo. `deploy/acceptance-slots.py` is the executable HTTP contract that plan will implement against.

---

## Task 1: `slots.earliest_fit` — when does one card fit this shape

**Files:**
- Create: `resourcemonitor/slots.py`
- Test: `tests/test_slots.py`

**Interfaces:**
- Consumes: `resourcemonitor.claims.Booking` (read-only: `gpu`, `vram_mib`, `start`, `end`, `cancelled_at`).
- Produces:
```python
HORIZON = timedelta(days=14)
def booked_at(bookings: list[Booking], gpu: int, t: datetime) -> int
def fits(bookings: list[Booking], gpu: int, vram_mib: int, start: datetime,
         duration: timedelta, card_mib: int) -> bool
def earliest_fit(bookings: list[Booking], gpu: int, vram_mib: int, duration: timedelta,
                 card_mib: int, now: datetime) -> datetime | None
```

**Note on the spec.** The spec writes `duration_s: int`; this plan uses `duration: timedelta`, because every other time value in `claims.py` (`DEFAULT_GRACE`, `PAST_SLACK`, `QUICK_MIN`) is a `timedelta` and mixing the two is how unit bugs happen. The spec also gives `earliest_fit` a `gpu=None` mode; that mode is `earliest_fit_group(count=1)` in Task 2, so there is one implementation of "which card" instead of two.

- [ ] **Step 1: Write the failing tests**

```python
"""Pure capacity arithmetic: no DB, no clock, no GPU."""
from datetime import datetime, timedelta, timezone

from resourcemonitor.claims import Booking
from resourcemonitor.slots import HORIZON, booked_at, earliest_fit, fits

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CARD = 81559
H = timedelta(hours=1)


def b(gpu=0, gib=20, start_h=0, hours=1, bid=1, cancelled=False):
    start = T0 + timedelta(hours=start_h)
    return Booking(id=bid, user="alice", gpu=gpu, vram_mib=gib * 1024, start=start,
                   end=start + timedelta(hours=hours), note=None, created_at=T0,
                   created_ip="10.0.0.1",
                   cancelled_at=T0 if cancelled else None)


def test_booked_at_sums_only_live_overlapping_rows_on_that_card():
    rows = [b(bid=1), b(bid=2), b(gpu=1, bid=3), b(bid=4, cancelled=True),
            b(bid=5, start_h=5)]
    assert booked_at(rows, 0, T0) == 40 * 1024
    assert booked_at(rows, 1, T0) == 20 * 1024
    assert booked_at(rows, 0, T0 + timedelta(hours=2)) == 0


def test_empty_card_fits_now():
    assert earliest_fit([], 0, 20 * 1024, H, CARD, T0) == T0


def test_a_full_card_fits_when_the_blocking_booking_ends():
    rows = [b(gib=79, hours=3)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=3)


def test_the_whole_window_must_fit_not_just_its_start():
    # free now, but a 79 GiB booking starts in 30 minutes: a 1 h slot cannot start now.
    rows = [b(gib=79, start_h=0.5, hours=2)]
    got = earliest_fit(rows, 0, 20 * 1024, H, CARD, T0)
    assert got == T0 + timedelta(hours=2, minutes=30)


def test_one_mib_too_much_waits_and_an_exact_fit_does_not():
    rows = [b(gib=61, hours=2)]           # 61 + 20 GiB = 82944 MiB, past the 81559 card
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=2)
    rows = [b(gib=59, hours=2)]           # 59 + 20 GiB = 80896 MiB, fits beside it now
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0


def test_a_gap_between_two_bookings_is_found():
    rows = [b(gib=79, hours=1, bid=1), b(gib=79, start_h=3, hours=5, bid=2)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=1)


def test_a_gap_too_short_for_the_duration_is_skipped():
    rows = [b(gib=79, hours=1, bid=1), b(gib=79, start_h=1.5, hours=5, bid=2)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=6, minutes=30)


def test_cancelled_rows_never_block():
    assert earliest_fit([b(gib=79, hours=9, cancelled=True)], 0, 20 * 1024, H, CARD, T0) == T0


def test_more_than_the_card_never_fits():
    assert earliest_fit([], 0, 90 * 1024, H, CARD, T0) is None


def test_nothing_beyond_the_horizon_is_offered():
    rows = [b(gib=79, hours=24 * 20)]     # 20 days
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) is None


def test_fits_is_exclusive_at_the_end_so_back_to_back_slots_work():
    rows = [b(gib=79, hours=1)]
    assert fits(rows, 0, 20 * 1024, T0 + H, H, CARD) is True
    assert fits(rows, 0, 20 * 1024, T0, H, CARD) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_slots.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'resourcemonitor.slots'`

- [ ] **Step 3: Write the implementation**

```python
"""When does a booking shape fit, and in what order may pending bookings move?

Pure arithmetic over Booking rows: no SQL, no clock of its own, no I/O. That is the
same seam probe.py sits behind -- it is why every rule in this file is testable on a
laptop with no GPU.

Booked VRAM on a card only *rises* at a booking's start and only *falls* at its end, so
the earliest instant a new window can begin is `now` or some booking's end. That finite
candidate set is the whole trick; there is no search and no loop over time."""
from __future__ import annotations

from datetime import datetime, timedelta

from resourcemonitor.claims import Booking

HORIZON = timedelta(days=14)        # matches claims.MAX_DAYS: never offer a slot past it


def _live(bookings: list[Booking], gpu: int) -> list[Booking]:
    return [b for b in bookings if b.gpu == gpu and b.cancelled_at is None]


def booked_at(bookings: list[Booking], gpu: int, t: datetime) -> int:
    """MiB booked on `gpu` at instant t, cancelled rows excluded."""
    return sum(b.vram_mib for b in _live(bookings, gpu) if b.start <= t < b.end)


def fits(bookings: list[Booking], gpu: int, vram_mib: int, start: datetime,
         duration: timedelta, card_mib: int) -> bool:
    """Does `vram_mib` fit on `gpu` for the whole of [start, start + duration)?
    Checking `start` and every booking start strictly inside the window covers every
    instant, because booked VRAM only rises at a start."""
    if vram_mib > card_mib:
        return False
    end = start + duration
    rows = _live(bookings, gpu)
    instants = [start] + sorted(b.start for b in rows if start < b.start < end)
    return all(booked_at(rows, gpu, t) + vram_mib <= card_mib for t in instants)


def earliest_fit(bookings: list[Booking], gpu: int, vram_mib: int, duration: timedelta,
                 card_mib: int, now: datetime) -> datetime | None:
    """The first instant at or after `now` where this shape fits on this card for its
    whole duration, or None if that is beyond HORIZON (or never)."""
    rows = _live(bookings, gpu)
    limit = now + HORIZON
    candidates = [now] + sorted({b.end for b in rows if now < b.end <= limit})
    for t in candidates:
        if fits(rows, gpu, vram_mib, t, duration, card_mib):
            return t
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_slots.py -v`
Expected: PASS, 11 tests. Then `python -m pytest -q` — the whole suite must still be 447 passed, 1 skipped; this task adds a module and imports nothing new into existing code.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/slots.py tests/test_slots.py
git commit -m "feat(slots): earliest_fit -- the first instant a shape fits one card"
```

---

## Task 2: `slots.earliest_fit_group` — atomic multi-card assignment

**Files:**
- Modify: `resourcemonitor/slots.py`
- Test: `tests/test_slots.py`

**Interfaces:**
- Consumes: `fits`, `HORIZON` from Task 1.
- Produces:
```python
def earliest_fit_group(bookings: list[Booking], count: int, vram_mib: int,
                       duration: timedelta, cards: dict[int, int], now: datetime,
                       gpus: list[int] | None = None) -> tuple[datetime, list[int]] | None
```
`cards` maps GPU index to that card's total MiB (t14 passes `{0: 16376}`, carrot eight entries). The return is one start and exactly `count` GPU indices, lowest-index first. `count=1` is how a single booking with `gpu=None` chooses its card.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_slots.py
from resourcemonitor.slots import earliest_fit_group

CARDS8 = {g: CARD for g in range(8)}


def test_group_waits_for_the_last_card_and_never_staggers():
    # cards 0..3 blocked for 1, 2, 3 and 4 hours; a 4-card group starts at +4h.
    rows = [b(gpu=g, gib=79, hours=g + 1, bid=g + 1) for g in range(4)]
    got = earliest_fit_group(rows, 4, 20 * 1024, H, CARDS8, T0)
    assert got == (T0 + timedelta(hours=4), [0, 1, 2, 3])


def test_group_prefers_free_cards_over_waiting():
    rows = [b(gpu=g, gib=79, hours=9, bid=g + 1) for g in range(4)]
    got = earliest_fit_group(rows, 4, 20 * 1024, H, CARDS8, T0)
    assert got == (T0, [4, 5, 6, 7])


def test_group_picks_lowest_indices_among_equals():
    assert earliest_fit_group([], 2, 20 * 1024, H, CARDS8, T0) == (T0, [0, 1])


def test_group_larger_than_the_box_never_fits():
    assert earliest_fit_group([], 9, 20 * 1024, H, CARDS8, T0) is None


def test_group_respects_a_gpus_restriction():
    assert earliest_fit_group([], 2, 20 * 1024, H, CARDS8, T0, gpus=[5, 6]) == (T0, [5, 6])


def test_count_one_is_how_a_floating_booking_chooses_its_card():
    rows = [b(gpu=0, gib=79, hours=3)]
    assert earliest_fit_group(rows, 1, 20 * 1024, H, CARDS8, T0) == (T0, [1])


def test_group_honours_per_card_sizes():
    # t14's single 16 GiB card cannot host 20 GiB at all.
    assert earliest_fit_group([], 1, 20 * 1024, H, {0: 16376}, T0) is None
    assert earliest_fit_group([], 1, 10 * 1024, H, {0: 16376}, T0) == (T0, [0])


def test_group_beyond_the_horizon_is_none():
    rows = [b(gpu=g, gib=79, hours=24 * 20, bid=g + 1) for g in range(8)]
    assert earliest_fit_group(rows, 2, 20 * 1024, H, CARDS8, T0) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_slots.py -k group -v`
Expected: FAIL — `ImportError: cannot import name 'earliest_fit_group'`

- [ ] **Step 3: Write the implementation**

```python
def earliest_fit_group(bookings: list[Booking], count: int, vram_mib: int,
                       duration: timedelta, cards: dict[int, int], now: datetime,
                       gpus: list[int] | None = None) -> tuple[datetime, list[int]] | None:
    """The earliest instant where `count` cards can each host `vram_mib` for the whole
    duration, with the lowest-index fitting cards chosen. All members share one start --
    a group assigned staggered starts would sit holding idle cards, which is the whole
    reason groups exist. count=1 is also how a booking with no chosen card picks one."""
    allowed = sorted(cards if gpus is None else [g for g in gpus if g in cards])
    if count < 1 or count > len(allowed):
        return None
    limit = now + HORIZON
    candidates = [now] + sorted({b.end for b in bookings
                                 if b.cancelled_at is None and now < b.end <= limit
                                 and b.gpu in cards})
    for t in candidates:
        free = [g for g in allowed
                if fits(bookings, g, vram_mib, t, duration, cards[g])]
        if len(free) >= count:
            return t, free[:count]
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_slots.py -v`
Expected: PASS, 19 tests.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/slots.py tests/test_slots.py
git commit -m "feat(slots): earliest_fit_group -- all members share one start"
```

---

## Task 3: Schema v3 → v4 — three columns on `claims`

**Files:**
- Modify: `resourcemonitor/claims.py:27` (`SCHEMA_VERSION`), `:38-56` (`_SCHEMA`), `:58-65` (column lists), `:67-98` (`Booking`), `:171-177` (`_row`), `:323-342` (`_migrate`), `:497-505` (`_read_ro` column choice)
- Test: `tests/test_claims.py` (migration tests appended)

**Interfaces:**
- Produces: `SCHEMA_VERSION = 4`; `Booking.auto_advance: bool = False`, `Booking.group_id: str | None = None`, `Booking.notice_seconds: int | None = None`, all three in `to_json()`. Every later task reads these.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_claims.py
import sqlite3

from resourcemonitor.claims import SCHEMA_VERSION, ClaimsStore

_V3_SCHEMA = """
CREATE TABLE claims (
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, gpu INTEGER NOT NULL,
  vram_mib INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, note TEXT,
  created_at TEXT NOT NULL, created_ip TEXT NOT NULL, cancelled_at TEXT,
  cancelled_ip TEXT, kind TEXT NOT NULL DEFAULT 'calendar',
  priority TEXT NOT NULL DEFAULT 'important');
PRAGMA user_version = 3;
"""


def _write_v3(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_V3_SCHEMA)
    conn.execute("INSERT INTO claims (user, gpu, vram_mib, start, end, created_at, "
                 "created_ip, priority) VALUES ('duser', 4, 40960, "
                 "'2026-10-06T12:00:00+00:00', '2026-10-06T16:00:00+00:00', "
                 "'2026-10-06T11:00:00+00:00', '10.0.0.1', 'lendable')")
    conn.commit()
    conn.close()


def test_schema_version_is_four():
    assert SCHEMA_VERSION == 4


def test_a_v3_file_migrates_and_keeps_every_row(tmp_path):
    path = tmp_path / "claims.sqlite"
    _write_v3(path)
    store = ClaimsStore(path, users=FakeUsers())
    try:
        got = store.get(1)
        assert got is not None
        assert got.user == "duser" and got.priority == "lendable"
        assert got.auto_advance is False
        assert got.group_id is None
        assert got.notice_seconds is None
        conn = sqlite3.connect(str(path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        conn.close()
    finally:
        store.close()


def test_migrating_twice_is_harmless(tmp_path):
    path = tmp_path / "claims.sqlite"
    _write_v3(path)
    for _ in range(2):
        store = ClaimsStore(path, users=FakeUsers())
        store.close()
    store = ClaimsStore(path, users=FakeUsers())
    try:
        assert store.get(1) is not None
    finally:
        store.close()


def test_new_columns_appear_in_json(tmp_path):
    store = ClaimsStore(tmp_path / "c.sqlite", users=FakeUsers())
    try:
        d = book(store).to_json()
        assert d["auto_advance"] is False
        assert d["group_id"] is None
        assert d["notice_seconds"] is None
    finally:
        store.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims.py -k "schema_version or v3_file or migrating_twice or new_columns" -v`
Expected: FAIL — `assert 3 == 4`, then `AttributeError: 'Booking' object has no attribute 'auto_advance'`

- [ ] **Step 3: Write the implementation**

In `resourcemonitor/claims.py`:

```python
SCHEMA_VERSION = 4
```

Add to `_SCHEMA`'s `CREATE TABLE claims` body, after `priority`:

```sql
  priority     TEXT    NOT NULL DEFAULT 'important',
  auto_advance INTEGER NOT NULL DEFAULT 0,
  group_id     TEXT,
  notice_seconds INTEGER
```

Replace the column-list block:

```python
_COLUMNS_V1 = ("id, user, gpu, vram_mib, start, end, note, created_at, created_ip, "
               "cancelled_at, cancelled_ip")
_COLUMNS = _COLUMNS_V1 + ", kind, priority, auto_advance, group_id, notice_seconds"
# Older files read before the web process migrated them: v1 has no kind (all calendar),
# v2 no priority, v3 none of the assignment columns. Rows from before priorities existed
# blocked everyone, so they read as important; rows from before assignment never move.
_COLUMNS_V3_AS_V4 = _COLUMNS_V1 + ", kind, priority, 0, NULL, NULL"
_COLUMNS_V2_AS_V4 = _COLUMNS_V1 + ", kind, 'important', 0, NULL, NULL"
_COLUMNS_V1_AS_V4 = _COLUMNS_V1 + ", 'calendar', 'important', 0, NULL, NULL"
```

Add to `Booking`, after `priority`:

```python
    auto_advance: bool = False              # may this booking move earlier by itself?
    group_id: str | None = None             # members of one atomic group share it
    notice_seconds: int | None = None       # warning this owner needs; None: nobody listens
```

Add to `Booking.to_json()`'s dict:

```python
            "auto_advance": self.auto_advance, "group_id": self.group_id,
            "notice_seconds": self.notice_seconds,
```

Replace `_row`:

```python
def _row(r: tuple) -> Booking:
    return Booking(id=r[0], user=r[1], gpu=r[2], vram_mib=r[3], start=_ts(r[4]),
                   end=_ts(r[5]), note=r[6], created_at=_ts(r[7]), created_ip=r[8],
                   cancelled_at=_ts(r[9]), cancelled_ip=r[10], kind=r[11],
                   priority=r[12], auto_advance=bool(r[13]), group_id=r[14],
                   notice_seconds=r[15])
```

In `_migrate`, extend the docstring and add the three columns beside the existing two:

```python
                if "auto_advance" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN auto_advance INTEGER "
                                 "NOT NULL DEFAULT 0")
                if "group_id" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN group_id TEXT")
                if "notice_seconds" not in cols:
                    conn.execute("ALTER TABLE claims ADD COLUMN notice_seconds INTEGER")
```

In `_read_ro`, replace the `select` choice:

```python
            select = (_COLUMNS if "notice_seconds" in cols else
                      _COLUMNS_V3_AS_V4 if "priority" in cols else
                      _COLUMNS_V2_AS_V4 if "kind" in cols else _COLUMNS_V1_AS_V4)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims.py tests/test_claims_priority.py tests/test_web_claims.py -q`
Expected: PASS. Then `python -m pytest -q` — the full suite stays green; nothing yet reads the new columns.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims.py
git commit -m "feat(claims): schema v4 adds auto_advance, group_id, notice_seconds"
```

---

## Task 4: `create(start=None)` — the server assigns the earliest slot

**Files:**
- Modify: `resourcemonitor/claims.py` — `ClaimError`, `Created`, `_validate`, `create`
- Test: `tests/test_claims_assign.py` (create), `tests/claims_fixture.py` (helper)

**Interfaces:**
- Consumes: `slots.earliest_fit`, `slots.earliest_fit_group` (Tasks 1–2).
- Produces:
```python
class ClaimError(Exception):
    def __init__(self, status: int, detail: str, extra: dict | None = None)
    # .status, .detail, .extra (always a dict; empty when none was given)

@dataclass(frozen=True)
class Created:
    bookings: list[Booking]
    takes: list[dict]
    adjusted_start: datetime | None = None
    assigned_later: bool = False
    revoked: list[dict] = field(default_factory=list)
    @property
    def booking(self) -> Booking   # bookings[0]; keeps every existing caller working

ClaimsStore.create(self, *, user, gpu: int | None, vram_mib, start: datetime | None,
                   end: datetime | None, note, ip, now,
                   card_mib=DEFAULT_CARD_MIB, priority="lendable", grace=DEFAULT_GRACE,
                   duration: timedelta | None = None, auto_advance: bool = False,
                   not_later_than: datetime | None = None,
                   cards: dict[int, int] | None = None) -> Created
```
`start=None` means "earliest". Exactly one of `end` and `duration` must be given. `gpu=None` means "any card". `cards` defaults to `{g: card_mib for g in range(GPU_COUNT)}`.

- [ ] **Step 1: Write the failing tests**

```python
"""The server choosing a start: POST without one never fails for lack of capacity."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)
CARD = 81559


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _assign(store, **over):
    d = dict(user="cwinkelmann", gpu=4, vram_mib=20 * 1024, start=None, end=None,
             duration=H, note=None, ip="10.0.0.1", now=T0)
    d.update(over)
    return store.create(**d)


def test_an_empty_card_is_assigned_now(store):
    made = _assign(store)
    assert made.booking.start == T0
    assert made.booking.end == T0 + H
    assert made.assigned_later is False


def test_a_full_card_is_assigned_the_first_free_instant(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    made = _assign(store)
    assert made.booking.start == T0 + timedelta(hours=3)
    assert made.assigned_later is True


def test_an_assigned_slot_is_an_ordinary_row_that_blocks_the_next_one(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    first = _assign(store, vram_mib=79 * 1024)
    second = _assign(store, vram_mib=79 * 1024)
    assert second.booking.start == first.booking.end


def test_gpu_none_floats_to_a_free_card(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    made = _assign(store, gpu=None)
    assert made.booking.start == T0
    assert made.booking.gpu == 0


def test_auto_advance_is_stored(store):
    made = _assign(store, auto_advance=True)
    assert store.get(made.booking.id).auto_advance is True


def test_an_explicit_start_still_conflicts_and_the_409_carries_the_offer(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    with pytest.raises(ClaimError) as e:
        _assign(store, start=T0, end=T0 + H, duration=None, vram_mib=20 * 1024)
    assert e.value.status == 409
    assert e.value.extra["earliest"] == (T0 + timedelta(hours=3)).isoformat()
    assert e.value.extra["free_now"] == CARD - 79 * 1024


def test_not_later_than_fails_fast_instead_of_sleeping_for_a_fortnight(store):
    book(store, gpu=4, gib=79, start=T0, hours=10)
    with pytest.raises(ClaimError) as e:
        _assign(store, not_later_than=T0 + timedelta(hours=2))
    assert e.value.status == 409
    assert e.value.extra["earliest"] == (T0 + timedelta(hours=10)).isoformat()


def test_more_than_the_card_is_a_400_not_an_endless_wait(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, vram_mib=90 * 1024)
    assert e.value.status == 400


def test_exactly_one_of_end_and_duration(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, end=T0 + H, duration=H)
    assert e.value.status == 400
    with pytest.raises(ClaimError):
        _assign(store, end=None, duration=None)


def test_nothing_fits_before_the_horizon(store):
    book(store, gpu=4, gib=79, start=T0, hours=24 * 20)
    with pytest.raises(ClaimError) as e:
        _assign(store)
    assert e.value.status == 409
    assert e.value.extra["earliest"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims_assign.py -v`
Expected: FAIL — `TypeError: create() got an unexpected keyword argument 'duration'`

- [ ] **Step 3: Write the implementation**

`ClaimError` gains an `extra` payload, so a 409 can carry the offer instead of only prose:

```python
class ClaimError(Exception):
    def __init__(self, status: int, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or {}
```

`Created` grows; `.booking` keeps every existing caller (web.py, test_claims.py, the fixture) working:

```python
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
        return self.bookings[0]
```

Add `from dataclasses import dataclass, field` to the imports.

Split the window rules out of `_validate` so an assigned window can be checked after it is
chosen rather than before:

```python
def _validate_window(start: datetime, end: datetime, now: datetime) -> None:
    if end <= start:
        raise ClaimError(400, "end must be after start")
    if start < now - PAST_SLACK:
        raise ClaimError(400, "start lies in the past")
    if end - start > timedelta(days=MAX_DAYS):
        raise ClaimError(400, f"a booking may last at most {MAX_DAYS} days")
```

and call it from `_validate` in place of those three checks, so existing behaviour is
unchanged for an explicit window.

In `create`, before the lock: resolve the shape, then let the transaction choose the start.

```python
    def create(self, *, user: str, gpu: int | None, vram_mib: int,
               start: datetime | None, end: datetime | None, note: str | None, ip: str,
               now: datetime, card_mib: int = DEFAULT_CARD_MIB,
               priority: str = "lendable", grace: timedelta = DEFAULT_GRACE,
               duration: timedelta | None = None, auto_advance: bool = False,
               not_later_than: datetime | None = None,
               cards: dict[int, int] | None = None) -> Created:
        """Book, or raise ClaimError. With start=None the server assigns the earliest slot
        that fits and the booking never fails for lack of capacity; with an explicit start
        the old behaviour is kept exactly, 409 included. An important booking that would
        squeeze a lendable booking already under way starts no earlier than now + grace."""
        if priority not in PRIORITIES:
            raise ClaimError(400, "priority must be lendable or important")
        if (end is None) == (duration is None):
            raise ClaimError(400, "give exactly one of end and duration")
        now = _utc(now)
        if duration is None:
            duration = _utc(end) - _utc(start if start is not None else now)
        if duration <= timedelta(0) or duration > timedelta(days=MAX_DAYS):
            raise ClaimError(400, f"a booking may last at most {MAX_DAYS} days")
        cards = cards or {g: card_mib for g in range(GPU_COUNT)}
        _validate_user(self.users, user)
        if gpu is not None:
            _validate_gpu(gpu)
            card_mib = cards.get(gpu, card_mib)
        if vram_mib < MIB_PER_GIB or vram_mib > max(cards.values()):
            raise ClaimError(400, f"VRAM must be between 1 and "
                                  f"{max(cards.values()) // MIB_PER_GIB} GiB")
        note = _validate_note(note)
        ...
```

`_validate_note` is the note half of today's `_validate`, lifted out so it can be called on
its own. Inside the transaction, where `overlapping` is read today, read the whole card set
when the start is not fixed:

```python
                if start is None:
                    rows = [_row(r) for r in conn.execute(
                        f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL "
                        "AND end > ?", (now.isoformat(),))]
                    gpus = None if gpu is None else [gpu]
                    hit = earliest_fit_group(rows, 1, vram_mib, duration, cards, now,
                                             gpus=gpus)
                    if hit is None:
                        raise ClaimError(409, "no slot fits within the next "
                                              f"{MAX_DAYS} days",
                                         {"earliest": None,
                                          "free_now": _free_now(rows, cards, gpu)})
                    start, chosen = hit[0], hit[1][0]
                    gpu, card_mib = chosen, cards[chosen]
                    if not_later_than is not None and start > _utc(not_later_than):
                        raise ClaimError(409, "the earliest slot is later than asked",
                                         {"earliest": start.isoformat(),
                                          "free_now": _free_now(rows, cards, gpu)})
                    assigned_later = start > now
                    end = start + duration
```

For an explicit start, `end` is already given; keep the existing `_check_capacity` call, and
attach the offer to its 409 by catching and re-raising:

```python
                else:
                    start, end = _utc(start), _utc(end)
                    _validate_window(start, end, now)
                    assigned_later = False
                    try:
                        _check_capacity(overlapping, gpu, vram_mib, start, end, card_mib,
                                        priority)
                    except ClaimError as e:
                        if e.status != 409:
                            raise
                        fit = earliest_fit(overlapping, gpu, vram_mib, duration, card_mib,
                                           now)
                        raise ClaimError(409, e.detail,
                                         {"earliest": None if fit is None else fit.isoformat(),
                                          "free_now": max(card_mib - booked_at(
                                              overlapping, gpu, start), 0)}) from None
```

with a module-level helper, called as `_free_now(rows, cards, gpu, now)`:

```python
def _free_now(rows: list[Booking], cards: dict[int, int], gpu: int | None,
              now: datetime) -> int:
    """The most MiB free on one card at `now` -- what "book less" would have to fit into.
    With gpu=None that is the best card, which is the number a floating booking cares about."""
    want = cards if gpu is None else {gpu: cards[gpu]}
    return max((mib - booked_at(rows, g, now) for g, mib in want.items()), default=0)
```

Import `booked_at`, `earliest_fit` and
`earliest_fit_group` from `resourcemonitor.slots` **inside** `claims.py` — note `slots.py`
imports `Booking` from `claims.py`, so the import must be local to the functions that use
it (`from resourcemonitor.slots import earliest_fit` inside `create`), or the two modules
form a cycle at import time. A deferred import inside the method body is the smallest fix
and the one this task takes.

Store the new columns in the INSERT:

```python
                cur = conn.execute(
                    "INSERT INTO claims (user, gpu, vram_mib, start, end, note, created_at, "
                    "created_ip, priority, auto_advance) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user, gpu, vram_mib, start.isoformat(), end.isoformat(), note,
                     now.isoformat(), ip, priority, int(auto_advance)))
```

and return `Created([booking], takes, asked if start != asked else None, assigned_later)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims_assign.py -v`
Expected: PASS, 11 tests.
Run: `python -m pytest -q`
Expected: PASS — `test_claims.py`, `test_claims_priority.py` and `test_web_claims.py` all
call `create(start=…, end=…)` positionally by keyword and keep working; `Created.booking`
keeps the old attribute. If any test fails on the `gpu: int | None` signature, fix the test
to pass `gpu=` explicitly rather than widening the signature further.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims_assign.py
git commit -m "feat(claims): a booking with no start is assigned the earliest slot"
```

---

## Task 5: Atomic group bookings

**Files:**
- Modify: `resourcemonitor/claims.py` — `create` gains `count`
- Test: `tests/test_claims_assign.py` (group tests appended)

**Interfaces:**
- Consumes: `slots.earliest_fit_group` (Task 2), `Created.bookings` (Task 4).
- Produces: `create(..., count: int = 1)`. With `count > 1`, `gpu` must be `None` and
  `start` must be `None`; the call returns `Created.bookings` with `count` rows sharing one
  `group_id` (a `uuid4().hex`) and one start.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_claims_assign.py
def test_four_cards_are_assigned_one_shared_start(store):
    made = _assign(store, gpu=None, count=4)
    starts = {b.start for b in made.bookings}
    assert len(made.bookings) == 4
    assert starts == {T0}
    assert sorted(b.gpu for b in made.bookings) == [0, 1, 2, 3]


def test_a_group_shares_one_group_id_and_singles_have_none(store):
    group = _assign(store, gpu=None, count=4)
    ids = {b.group_id for b in group.bookings}
    assert len(ids) == 1 and ids != {None}
    single = _assign(store, gpu=None, count=1)
    assert single.booking.group_id is None


def test_a_group_waits_for_the_last_card(store):
    for g in range(4):
        book(store, gpu=g, gib=79, start=T0, hours=g + 1)
    made = _assign(store, gpu=None, count=4)
    assert {b.start for b in made.bookings} == {T0 + timedelta(hours=4)}
    assert made.assigned_later is True


def test_a_group_larger_than_the_box_is_a_400(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=None, count=9)
    assert e.value.status == 400


def test_count_must_be_a_whole_number_above_zero(store):
    for bad in (0, -1, 1.5, True, "4"):
        with pytest.raises(ClaimError) as e:
            _assign(store, gpu=None, count=bad)
        assert e.value.status == 400


def test_a_group_cannot_name_one_card_or_one_start(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=4, count=2)
    assert e.value.status == 400
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=None, count=2, start=T0, end=T0 + H, duration=None)
    assert e.value.status == 400


def test_every_group_member_is_an_ordinary_row_the_next_booking_respects(store):
    _assign(store, gpu=None, count=8, vram_mib=79 * 1024)
    later = _assign(store, gpu=None, vram_mib=79 * 1024)
    assert later.booking.start == T0 + H
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims_assign.py -k group -v`
Expected: FAIL — `TypeError: create() got an unexpected keyword argument 'count'`

- [ ] **Step 3: Write the implementation**

Add `import uuid` to `claims.py`. In `create`, validate the group shape beside the other
pre-lock checks:

```python
        if not _is_count(count):
            raise ClaimError(400, "count must be a whole number of GPUs, 1 or more")
        if count > GPU_COUNT:
            raise ClaimError(400, f"count must be at most {GPU_COUNT}")
        if count > 1 and (gpu is not None or start is not None):
            raise ClaimError(400, "a group booking cannot name a GPU or a start")
```

with

```python
def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1
```

Inside the transaction, the group branch replaces the single `earliest_fit_group(…, 1, …)`
call with the same call at `count`, then inserts one row per chosen card:

```python
                    hit = earliest_fit_group(rows, count, vram_mib, duration, cards, now,
                                             gpus=None if gpu is None else [gpu])
                    ...
                    start, chosen = hit
                    group_id = uuid.uuid4().hex if count > 1 else None
                    made = []
                    for g in chosen:
                        cur = conn.execute(
                            "INSERT INTO claims (user, gpu, vram_mib, start, end, note, "
                            "created_at, created_ip, priority, auto_advance, group_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (user, g, vram_mib, start.isoformat(),
                             (start + duration).isoformat(), note, now.isoformat(), ip,
                             priority, int(auto_advance), group_id))
                        made.append(Booking(id=cur.lastrowid, user=user, gpu=g,
                                            vram_mib=vram_mib, start=start,
                                            end=start + duration, note=note,
                                            created_at=now, created_ip=ip,
                                            priority=priority, auto_advance=auto_advance,
                                            group_id=group_id))
```

The group is assigned against one `rows` snapshot inside one `BEGIN IMMEDIATE`, which is
what makes it atomic: no second writer can take the third card between members two and four.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims_assign.py -v`
Expected: PASS, 18 tests. Then `python -m pytest -q` — full suite green.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims_assign.py
git commit -m "feat(claims): atomic group bookings share one group_id and one start"
```

---

## Task 6: `release` — truncate, don't delete

**Files:**
- Modify: `resourcemonitor/claims.py` — new `Released`, `release`, group cascade in `cancel`
- Test: `tests/test_claims_release.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class Released:
    booking: Booking
    advanced: list[dict] = field(default_factory=list)

ClaimsStore.release(self, claim_id: int, *, end: datetime | None, ip: str, now: datetime,
                    cards: dict[int, int] | None = None) -> Released
ClaimsStore.cancel(self, claim_id: int, *, ip: str, now: datetime,
                   cards: dict[int, int] | None = None) -> Booking   # return type unchanged
```
`advanced` is filled by Task 7; this task leaves it empty.

- [ ] **Step 1: Write the failing tests**

```python
"""Giving a card back honestly: the window used stays on the calendar."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def test_a_started_booking_is_truncated_not_cancelled(store):
    b = book(store, start=T0, hours=4)
    out = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=40))
    assert out.booking.end == T0 + timedelta(minutes=40)
    assert out.booking.cancelled_at is None       # it still happened; energy still counts


def test_a_not_yet_started_booking_falls_back_to_cancel(store):
    b = book(store, start=T0 + timedelta(hours=2), hours=4, now=T0)
    out = store.release(b.id, end=None, ip="10.0.0.1", now=T0)
    assert out.booking.cancelled_at == T0
    assert out.booking.end == b.end                # untouched: end < start would be nonsense


def test_truncation_frees_the_tail_for_someone_else(store):
    b = book(store, gpu=4, gib=79, start=T0, hours=4)
    store.release(b.id, end=None, ip="10.0.0.1", now=T0 + H)
    made = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None,
                        end=None, duration=H, note=None, ip="10.0.0.1", now=T0 + H)
    assert made.booking.start == T0 + H


def test_releasing_an_already_ended_booking_is_a_no_op(store):
    b = book(store, start=T0, hours=1)
    first = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=30))
    again = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=50))
    assert again.booking.end == first.booking.end


def test_releasing_a_cancelled_booking_is_a_409(store):
    b = book(store, start=T0, hours=4)
    store.cancel(b.id, ip="10.0.0.1", now=T0)
    with pytest.raises(ClaimError) as e:
        store.release(b.id, end=None, ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 409


def test_an_unknown_booking_is_a_404(store):
    with pytest.raises(ClaimError) as e:
        store.release(9999, end=None, ip="10.0.0.1", now=T0)
    assert e.value.status == 404


def test_an_end_before_the_start_is_a_400(store):
    b = book(store, start=T0, hours=4)
    with pytest.raises(ClaimError) as e:
        store.release(b.id, end=T0 - H, ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 400


def test_extending_rechecks_capacity_and_can_lose_to_an_assigned_slot(store):
    mine = book(store, gpu=4, gib=79, start=T0, hours=2)
    store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None, end=None,
                 duration=H, note=None, ip="10.0.0.1", now=T0)      # lands at T0+2h
    with pytest.raises(ClaimError) as e:
        store.release(mine.id, end=T0 + timedelta(hours=3), ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 409


def test_extending_into_free_space_succeeds(store):
    b = book(store, gpu=4, gib=79, start=T0, hours=2)
    out = store.release(b.id, end=T0 + timedelta(hours=3), ip="10.0.0.1", now=T0 + H)
    assert out.booking.end == T0 + timedelta(hours=3)


def test_cancelling_a_not_yet_started_group_member_cancels_the_set(store):
    made = store.create(user="cwinkelmann", gpu=None, vram_mib=20 * 1024, start=None,
                        end=None, duration=H, note=None, ip="10.0.0.1", now=T0, count=4)
    store.cancel(made.bookings[0].id, ip="10.0.0.1", now=T0 - timedelta(minutes=1))
    assert all(store.get(b.id).cancelled_at is not None for b in made.bookings)


def test_releasing_an_under_way_group_member_truncates_only_that_row(store):
    made = store.create(user="cwinkelmann", gpu=None, vram_mib=20 * 1024, start=None,
                        end=None, duration=timedelta(hours=4), note=None, ip="10.0.0.1",
                        now=T0, count=4)
    store.release(made.bookings[0].id, end=None, ip="10.0.0.1", now=T0 + H)
    assert store.get(made.bookings[0].id).end == T0 + H
    assert store.get(made.bookings[1].id).end == T0 + timedelta(hours=4)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims_release.py -v`
Expected: FAIL — `AttributeError: 'ClaimsStore' object has no attribute 'release'`

- [ ] **Step 3: Write the implementation**

```python
@dataclass(frozen=True)
class Released:
    """A released booking and any assigned slots that moved earlier because of it."""
    booking: Booking
    advanced: list[dict] = field(default_factory=list)
```

```python
    def release(self, claim_id: int, *, end: datetime | None, ip: str, now: datetime,
                cards: dict[int, int] | None = None) -> Released:
        """Give a card back honestly. A booking under way has its end moved, so the window
        it really used stays on the calendar and in the energy ledger; a booking that has
        not started is cancelled instead, because end < start is a nonsense row. Moving the
        end *later* is an extension and re-checks capacity."""
        now = _utc(now)
        new_end = now if end is None else _utc(end)
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
                if b.start > now:
                    conn.execute("COMMIT")
                    return Released(self._cancel_locked_or_outer(claim_id, ip, now, cards))
                if b.end <= now:
                    conn.execute("COMMIT")
                    return Released(b)              # already over: nothing to give back
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
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return Released(out)
```

The `start > now` branch must not call `self.cancel()` while holding `self._lock` — that
would deadlock on a non-reentrant lock. Restructure instead: do the lookup in a short
transaction, release the lock, then delegate:

```python
        b = self.get(claim_id)
        if b is None:
            raise ClaimError(404, "no such booking")
        if b.cancelled_at is not None:
            raise ClaimError(409, "already cancelled")
        if b.start > now:
            return Released(self.cancel(claim_id, ip=ip, now=now, cards=cards))
        # ... then the locked transaction above, without the 404/409/start branches
```

The row is re-read inside the transaction anyway, so the pre-check is an early exit, not a
race: a row that changes in between is caught by the in-transaction checks.

`cancel` gains the group cascade and the `cards` parameter (unused until Task 7). Inside its
existing transaction, before the `UPDATE`:

```python
                row = conn.execute("SELECT group_id, start FROM claims WHERE id = ?",
                                   (claim_id,)).fetchone()
                if row is None:
                    raise ClaimError(404, "no such booking")
                group_id, start = row[0], _ts(row[1])
                if group_id is not None and start > now:
                    # A group exists so four cards are held together; a partial group would
                    # hold idle cards waiting for a member that is already gone.
                    conn.execute("UPDATE claims SET cancelled_at = ?, cancelled_ip = ? "
                                 "WHERE group_id = ? AND cancelled_at IS NULL "
                                 "AND start > ?",
                                 (now.isoformat(), ip, group_id, now.isoformat()))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims_release.py -v`
Expected: PASS, 11 tests. Then `python -m pytest -q` — full suite green, `cancel`'s return
type is unchanged.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims_release.py
git commit -m "feat(claims): release truncates a started booking, cancels a pending one"
```

---

## Task 7: Advancing — a slot moves earlier inside the releasing transaction

**Files:**
- Modify: `resourcemonitor/slots.py` (`advance_order`), `resourcemonitor/claims.py` (`_advance`, called from `release` and `cancel`)
- Test: `tests/test_slots.py` (order), `tests/test_claims_advance.py` (the move)

**Interfaces:**
- Produces:
```python
# slots.py
def advance_order(bookings: list[Booking]) -> list[Booking]
# claims.py, private
def _advance(conn, now: datetime, cards: dict[int, int]) -> list[dict]
# each dict: {"id", "user", "gpu", "old_start", "new_start"} with ISO strings for the times
```

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_slots.py
from resourcemonitor.slots import advance_order


def _pending(bid, priority="lendable", start_h=1, created_h=0):
    start = T0 + timedelta(hours=start_h)
    return Booking(id=bid, user="alice", gpu=0, vram_mib=20 * 1024, start=start,
                   end=start + H, note=None, created_at=T0 + timedelta(hours=created_h),
                   created_ip="10.0.0.1", priority=priority, auto_advance=True)


def test_important_moves_before_lendable():
    rows = [_pending(1), _pending(2, priority="important")]
    assert [b.id for b in advance_order(rows)] == [2, 1]


def test_within_a_class_the_earlier_start_moves_first():
    rows = [_pending(1, start_h=5), _pending(2, start_h=2)]
    assert [b.id for b in advance_order(rows)] == [2, 1]


def test_an_equal_start_is_broken_by_who_asked_first():
    rows = [_pending(1, created_h=3), _pending(2, created_h=1)]
    assert [b.id for b in advance_order(rows)] == [2, 1]
```

```python
"""A slot moves earlier only inside the transaction of a write that frees capacity."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import GPU_COUNT, ClaimsStore, DEFAULT_CARD_MIB
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)
CARDS = {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _assign(store, now=T0, **over):
    d = dict(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None, end=None,
             duration=H, note=None, ip="10.0.0.1", now=now, auto_advance=True,
             cards=CARDS)
    d.update(over)
    return store.create(**d).booking


def test_an_early_release_pulls_the_next_slot_earlier(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    assert waiting.start == T0 + timedelta(hours=4)
    out = store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == T0 + H
    assert out.advanced == [{"id": waiting.id, "user": "cwinkelmann", "gpu": 4,
                             "old_start": (T0 + timedelta(hours=4)).isoformat(),
                             "new_start": (T0 + H).isoformat()}]


def test_duration_is_preserved_and_the_id_is_kept(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store, duration=timedelta(hours=2))
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    moved = store.get(waiting.id)
    assert moved.id == waiting.id
    assert moved.end - moved.start == timedelta(hours=2)


def test_a_booking_without_auto_advance_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store, auto_advance=False)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == waiting.start


def test_a_booking_already_under_way_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    running = _assign(store, start=T0, end=T0 + timedelta(hours=9), duration=None,
                      gpu=5, vram_mib=20 * 1024)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(running.id).start == T0


def test_a_cancelled_slot_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    store.cancel(waiting.id, ip="10.0.0.1", now=T0, cards=CARDS)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == waiting.start


def test_a_natural_end_moves_nothing_because_nothing_was_freed(store):
    book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    # time simply passes: no write, so no advance, and the slot is where it was priced.
    assert store.get(waiting.id).start == T0 + timedelta(hours=4)


def test_advances_cascade_against_the_updated_timeline(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    first = _assign(store)
    second = _assign(store)
    assert (first.start, second.start) == (T0 + timedelta(hours=4),
                                           T0 + timedelta(hours=5))
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(first.id).start == T0 + H
    assert store.get(second.id).start == T0 + timedelta(hours=2)


def test_a_cancel_advances_too(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    assert waiting.start == T0 + timedelta(hours=4)
    store.cancel(blocker.id, ip="10.0.0.1", now=T0, cards=CARDS)
    assert store.get(waiting.id).start == T0


def test_a_group_moves_only_as_a_whole(store):
    blockers = [book(store, gpu=g, gib=79, start=T0, hours=4) for g in range(4)]
    group = store.create(user="cwinkelmann", gpu=None, vram_mib=79 * 1024, start=None,
                         end=None, duration=H, note=None, ip="10.0.0.1", now=T0,
                         count=4, auto_advance=True, cards=CARDS).bookings
    assert {b.start for b in group} == {T0 + timedelta(hours=4)}
    store.release(blockers[0].id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert {store.get(b.id).start for b in group} == {T0 + timedelta(hours=4)}
    for blocker in blockers[1:]:
        store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert {store.get(b.id).start for b in group} == {T0 + H}


def test_a_slot_never_advances_into_the_past(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start >= T0 + H
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_slots.py -k advance_order tests/test_claims_advance.py -v`
Expected: FAIL — `ImportError: cannot import name 'advance_order'`

- [ ] **Step 3: Write the implementation**

In `slots.py`:

```python
def advance_order(bookings: list[Booking]) -> list[Booking]:
    """The order in which pending bookings get offered freed capacity: important first,
    then the earliest start, then whoever asked first. One group member stands for its
    group, which is why the id is not part of the key."""
    return sorted(bookings, key=lambda b: (b.priority != "important", b.start,
                                           b.created_at, b.id))
```

In `claims.py`, one private helper called at the end of `release`'s and `cancel`'s
transaction, before `COMMIT`:

```python
    def _advance(self, conn, now: datetime, cards: dict[int, int]) -> list[dict]:
        """Move every advanceable booking to the earliest slot that now fits, re-reading
        the timeline after each move so advances cascade. Called only from inside the
        transaction of a write that freed capacity -- time passing can never make a slot
        advanceable, because earliest_fit already priced in every known end."""
        from resourcemonitor.slots import advance_order, earliest_fit, earliest_fit_group
        moved: list[dict] = []
        rows = [_row(r) for r in conn.execute(
            f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL AND end > ?",
            (now.isoformat(),))]
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
            rows = [_row(r) for r in conn.execute(
                f"SELECT {_COLUMNS} FROM claims WHERE cancelled_at IS NULL AND end > ?",
                (now.isoformat(),))]
        return moved
```

In `release`, replace `conn.execute("COMMIT")` on the success path with:

```python
                moved = self._advance(conn, now, cards or
                                      {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)})
                conn.execute("COMMIT")
```

and return `Released(out, moved)`. In `cancel`, call `self._advance(...)` the same way and
discard the list — the cancel route does not report moves, and the dashboard refetches.

`earliest_fit` returns an instant at or after `now`, so a slot can never advance into the
past; the `new_start >= b.start` guard means it never moves later either.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_slots.py tests/test_claims_advance.py -v`
Expected: PASS, 13 tests. Then `python -m pytest -q` — full suite green.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/slots.py resourcemonitor/claims.py tests/test_slots.py tests/test_claims_advance.py
git commit -m "feat(claims): an early release pulls the next assigned slot earlier"
```

---

## Task 8: `POST /api/claims` — the new request fields

**Files:**
- Modify: `resourcemonitor/web.py:50` (`_CLAIM_FIELDS`), `:309-337` (`_create`), `:347-362` (`_card_mib` → add `_cards`), `:233-243` (the 201 body), `:251-253` (error rendering)
- Test: `tests/test_web_claims.py`

**Interfaces:**
- Consumes: `ClaimsStore.create` (Tasks 4–5), `ClaimError.extra` (Task 4).
- Produces: the HTTP contract `deploy/acceptance-slots.py` (Task 16) and the SDK depend on:

```
POST /api/claims  {user, gpu?, vram_gib?, start?, end?, duration_minutes?, note?,
                   priority?, auto_advance?, not_later_than?, count?, notice_seconds?}
  201 {claim, claims:[…], takes:[…], adjusted_start, assigned_later, revoked:[…]}
  409 {error:"conflict", detail, earliest, free_now}
```
`start` omitted means earliest; `gpu` omitted or null means any card; exactly one of `end`
and `duration_minutes`.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_web_claims.py
def test_a_booking_without_a_start_is_assigned_and_says_so(live):
    status, _, body = _post(live + "/api/claims",
                            _booking(start=None, end=None, duration_minutes=60))
    assert status == 201
    d = json.loads(body)
    assert d["claim"]["start"] is not None
    assert d["assigned_later"] is False
    assert d["claims"] == [d["claim"]]


def test_an_assigned_booking_lands_after_a_blocker(live):
    blocker = json.loads(_post(live + "/api/claims",
                               _booking(vram_gib=79, start=S, end=E))[2])["claim"]
    status, _, body = _post(live + "/api/claims",
                            _booking(user="cwinkelmann", vram_gib=79, start=None, end=None,
                                     duration_minutes=60))
    assert status == 201
    d = json.loads(body)
    assert d["assigned_later"] is True
    assert d["claim"]["start"] == blocker["end"]


def test_a_group_booking_returns_every_member(live):
    status, _, body = _post(live + "/api/claims",
                            _booking(gpu=None, count=4, vram_gib=20, start=None, end=None,
                                     duration_minutes=60))
    assert status == 201
    d = json.loads(body)
    assert len(d["claims"]) == 4
    assert len({c["group_id"] for c in d["claims"]}) == 1


def test_a_conflict_carries_the_earliest_offer(live):
    _post(live + "/api/claims", _booking(vram_gib=79, start=S, end=E))
    status, _, body = _post(live + "/api/claims",
                            _booking(user="cwinkelmann", vram_gib=79, start=S, end=E))
    assert status == 409
    d = json.loads(body)
    assert d["error"] == "conflict"
    assert "earliest" in d and "free_now" in d


@pytest.mark.parametrize("minutes", [0, -5, 1.5, True, "60", 14 * 24 * 60 + 1])
def test_a_bad_duration_is_rejected_before_any_capacity_scan(live, minutes):
    status, _, body = _post(live + "/api/claims",
                            _booking(start=None, end=None, duration_minutes=minutes))
    assert status == 400
    assert json.loads(body)["error"] == "invalid"


def test_giving_both_end_and_duration_is_rejected(live):
    status, _, _ = _post(live + "/api/claims",
                         _booking(start=S, end=E, duration_minutes=60))
    assert status == 400


def test_auto_advance_and_notice_seconds_round_trip(live):
    status, _, body = _post(live + "/api/claims",
                            _booking(start=None, end=None, duration_minutes=60,
                                     auto_advance=True, notice_seconds=0))
    assert status == 201
    claim = json.loads(body)["claim"]
    assert claim["auto_advance"] is True
    assert claim["notice_seconds"] == 0


@pytest.mark.parametrize("field,value", [("auto_advance", "yes"), ("count", 0),
                                         ("count", 99), ("notice_seconds", -1),
                                         ("notice_seconds", "0"), ("gpu", 99)])
def test_malformed_new_fields_are_400_not_500(live, field, value):
    body = _booking(start=None, end=None, duration_minutes=60)
    body[field] = value
    status, _, _ = _post(live + "/api/claims", body)
    assert status == 400


def test_an_unknown_field_is_still_rejected(live):
    status, _, _ = _post(live + "/api/claims",
                         dict(_booking(), surprise=1))
    assert status == 400
```

`_booking()` must stop forcing `start`/`end`: change the helper so a key whose value is
`None` is dropped from the body.

```python
def _booking(**over):
    d = {"user": "duser", "gpu": 4, "vram_gib": 40, "start": S, "end": E}
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_claims.py -k "assigned or group_booking or earliest_offer or bad_duration or notice" -v`
Expected: FAIL — `400 {"error": "invalid", "detail": "unknown field"}` for the new names.

- [ ] **Step 3: Write the implementation**

```python
_CLAIM_FIELDS = ("user", "gpu", "vram_gib", "start", "end", "note", "priority",
                 "auto_advance", "duration_minutes", "not_later_than", "count",
                 "notice_seconds")
MAX_DURATION_MIN = 14 * 24 * 60
```

```python
    def _create(self, d: dict):
        if set(d) - set(_CLAIM_FIELDS):
            raise ClaimError(400, "unknown field")
        user = _field(d, "user", lambda v: isinstance(v, str))
        gpu = None if d.get("gpu") is None else _field(d, "gpu", _is_int)
        vram_gib = None if d.get("vram_gib") is None else _field(d, "vram_gib", _is_gib)
        start = None if d.get("start") is None else parse_time(d["start"])
        end = None if d.get("end") is None else parse_time(d["end"])
        not_later_than = None if d.get("not_later_than") is None \
            else parse_time(d["not_later_than"])
        duration = None
        if d.get("duration_minutes") is not None:
            minutes = _field(d, "duration_minutes",
                             lambda v: _is_int(v) and 1 <= v <= MAX_DURATION_MIN)
            duration = timedelta(minutes=minutes)
        note = d.get("note")
        if note is not None and not isinstance(note, str):
            raise ClaimError(400, "missing or invalid field 'note'")
        priority = d.get("priority", "lendable")
        if not isinstance(priority, str):
            raise ClaimError(400, "priority must be lendable or important")
        auto_advance = d.get("auto_advance", False)
        if not isinstance(auto_advance, bool):
            raise ClaimError(400, "auto_advance must be true or false")
        count = d.get("count", 1)
        if not _is_int(count) or not 1 <= count <= GPU_COUNT:
            raise ClaimError(400, f"count must be a whole number of GPUs, 1..{GPU_COUNT}")
        notice = d.get("notice_seconds")
        if notice is not None and (not _is_int(notice) or notice < 0):
            raise ClaimError(400, "notice_seconds must be a whole number of seconds, 0 or more")
        cards = self._cards()
        card_mib = cards.get(gpu, DEFAULT_CARD_MIB) if gpu is not None \
            else max(cards.values(), default=DEFAULT_CARD_MIB)
        vram_mib = card_mib if vram_gib is None else round(vram_gib * MIB_PER_GIB)
        return self._store().create(user=user, gpu=gpu, vram_mib=vram_mib, start=start,
                                    end=end, duration=duration, note=note,
                                    ip=self.client_address[0], now=self.server.clock(),
                                    card_mib=card_mib, priority=priority,
                                    grace=self._grace(), auto_advance=auto_advance,
                                    not_later_than=not_later_than, count=count,
                                    notice_seconds=notice, cards=cards,
                                    max_pending=self._max_pending(),
                                    watching=self.server.watchers.attached)
```

Three of those arguments do not exist on `create` yet: `max_pending` arrives in Task 10,
and `notice_seconds` and `watching` in Task 12. Leave them out of the call here and add each
one with its own task — the tests above that mention `notice_seconds` therefore belong to
Task 12's run, so move them there if this task is implemented on its own.

`_cards()` beside the existing `_card_mib()` (keep `_card_mib`, `_quick` still uses it):

```python
    def _cards(self) -> dict[int, int]:
        """Each GPU's total MiB from the latest poll; the default card size for any the
        history has never seen. One query instead of eight, since assignment needs them all.
        t14 answers {0: 16376}; carrot answers eight entries."""
        cards = {}
        try:
            conn = self._conn()
            try:
                for gpu, total in conn.execute(
                        "SELECT gpu, total_mib FROM gpu_samples "
                        "WHERE ts = (SELECT MAX(ts) FROM polls)"):
                    if 0 <= gpu < GPU_COUNT and total:
                        cards[gpu] = total
            finally:
                conn.close()
        except (NoHistory, sqlite3.Error):
            pass
        return cards or {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}
```

The 201 body:

```python
                made = self._create(body)
                self._json(201, {
                    "claim": made.booking.to_json(),
                    "claims": [b.to_json() for b in made.bookings],
                    "takes": [{**t, "start": t["start"].isoformat(),
                               "end": t["end"].isoformat(),
                               "vram_gib": round(t["vram_mib"] / MIB_PER_GIB, 1)}
                              for t in made.takes],
                    "adjusted_start": None if made.adjusted_start is None
                    else made.adjusted_start.isoformat(),
                    "assigned_later": made.assigned_later,
                    "revoked": made.revoked})
```

Error rendering carries the extras without letting them overwrite the fixed shape:

```python
        except ClaimError as e:
            body = {"error": _CLAIM_ERRORS[e.status], "detail": e.detail}
            body.update({k: v for k, v in e.extra.items() if k not in body})
            self._json(e.status, body)
```

Both `do_GET`'s and `do_POST`'s handlers render `ClaimError` this way; update both.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_claims.py -v`
Expected: PASS, including the whole existing file — the old explicit-window path is
unchanged. Then `python -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/web.py tests/test_web_claims.py
git commit -m "feat(web): POST /api/claims accepts duration, count, auto_advance"
```

---

## Task 9: `POST /api/claims/{id}/release` and `GET /api/capacity`

**Files:**
- Modify: `resourcemonitor/web.py:49` (route patterns), `:214-250` (`do_POST` dispatch), `:173-186` (`do_GET` handler map), new `_release`, `_capacity`
- Test: `tests/test_web_claims.py`

**Interfaces:**
- Consumes: `ClaimsStore.release` (Task 6), `slots.earliest_fit_group` (Task 2), `_cards()` (Task 8).
- Produces:
```
POST /api/claims/{id}/release  {end?: "now" | ISO-8601}
  200 {claim, advanced:[{id,user,gpu,old_start,new_start}]}   404 / 409 / 400 as usual
GET  /api/capacity?gpu=&vram_gib=&duration_minutes=&count=
  200 {free_mib, free_gib, earliest, gpu, gpus}
```

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_web_claims.py
def _claim_id(live, **over):
    return json.loads(_post(live + "/api/claims", _booking(**over))[2])["claim"]["id"]


def test_release_truncates_and_reports_nothing_advanced(live):
    cid = _claim_id(live, start=None, end=None, duration_minutes=240, vram_gib=79)
    status, _, body = _post(f"{live}/api/claims/{cid}/release", {})
    assert status == 200
    d = json.loads(body)
    assert d["claim"]["cancelled_at"] is None
    assert d["advanced"] == []


def test_release_reports_the_slot_it_pulled_earlier(live):
    blocker = _claim_id(live, start=None, end=None, duration_minutes=240, vram_gib=79)
    waiting = json.loads(_post(live + "/api/claims", _booking(
        user="cwinkelmann", gpu=4, vram_gib=79, start=None, end=None,
        duration_minutes=60, auto_advance=True))[2])["claim"]
    status, _, body = _post(f"{live}/api/claims/{blocker}/release", {})
    assert status == 200
    advanced = json.loads(body)["advanced"]
    assert [a["id"] for a in advanced] == [waiting["id"]]
    assert advanced[0]["new_start"] < waiting["start"]


def test_release_accepts_an_explicit_end(live):
    cid = _claim_id(live, start=S, end=E)
    status, _, body = _post(f"{live}/api/claims/{cid}/release",
                            {"end": "2026-10-06T15:00:00+02:00"})
    assert status == 200
    assert json.loads(body)["claim"]["end"] == "2026-10-06T13:00:00+00:00"


def test_releasing_an_unknown_booking_is_404(live):
    status, _, _ = _post(f"{live}/api/claims/999999/release", {})
    assert status == 404


def test_release_rejects_an_unknown_field(live):
    cid = _claim_id(live, start=S, end=E)
    status, _, _ = _post(f"{live}/api/claims/{cid}/release", {"when": "now"})
    assert status == 400


def test_release_is_a_post_only_route(live):
    cid = _claim_id(live, start=S, end=E)
    status, _, _ = _get(f"{live}/api/claims/{cid}/release")
    assert status in (404, 405)


def test_capacity_answers_free_now_and_the_earliest_fit(live):
    _post(live + "/api/claims", _booking(vram_gib=79, start=S, end=E))
    status, _, body = _get(live + "/api/capacity?gpu=4&vram_gib=79&duration_minutes=60")
    assert status == 200
    d = json.loads(body)
    assert d["gpu"] == 4
    assert d["free_mib"] < 79 * 1024
    assert d["earliest"] is not None


def test_capacity_without_a_gpu_considers_every_card(live):
    status, _, body = _get(live + "/api/capacity?vram_gib=20&duration_minutes=60")
    assert status == 200
    d = json.loads(body)
    assert d["gpu"] is not None
    assert d["earliest"] is not None


def test_capacity_rejects_rubbish(live):
    for q in ("?gpu=nine", "?vram_gib=-1", "?duration_minutes=0", "?count=99",
              "?surprise=1"):
        status, _, _ = _get(live + "/api/capacity" + q)
        assert status == 400
```

`_get` already exists in the file for the read tests; if not, add the three-line
`urllib.request` helper beside `_post`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_claims.py -k "release or capacity" -v`
Expected: FAIL — `404 {"error": "not found"}`

- [ ] **Step 3: Write the implementation**

```python
_RELEASE = re.compile(r"^/api/claims/(\d{1,9})/release$", re.ASCII)
```

In `do_POST`, beside the cancel match:

```python
        m_cancel = _CANCEL.match(path)
        m_release = _RELEASE.match(path)
        if path not in ("/api/claims", _QUICK) and m_cancel is None and m_release is None:
            self._drain()
            self._json(404, {"error": "not found"})
            return
```

and in the dispatch chain, before the cancel branch:

```python
            elif m_release is not None:
                out = self._release(int(m_release.group(1)), body)
                self._json(200, {"claim": out.booking.to_json(), "advanced": out.advanced})
```

```python
    def _release(self, claim_id: int, d: dict):
        if set(d) - {"end"}:
            raise ClaimError(400, "unknown field")
        raw = d.get("end")
        if raw is not None and not isinstance(raw, str):
            raise ClaimError(400, "missing or invalid field 'end'")
        end = None if raw in (None, "now") else parse_time(raw)
        return self._store().release(claim_id, end=end, ip=self.client_address[0],
                                     now=self.server.clock(), cards=self._cards())
```

Add `"/api/capacity": self._capacity` to `do_GET`'s handler map, and add `/api/capacity`
to `_GET_ONLY` so a POST to it answers 405 rather than 404.

```python
    def _capacity(self, query: str) -> None:
        """The one authoritative capacity answer: what fits now, and when this shape fits.
        It exists because clients were re-implementing the VRAM sum and getting it wrong."""
        from resourcemonitor.slots import booked_at, earliest_fit_group
        p = _params(query, {"gpu", "vram_gib", "duration_minutes", "count"})
        cards = self._cards()
        gpu = None
        if "gpu" in p:
            if not p["gpu"].isdigit() or int(p["gpu"]) not in cards:
                raise ClaimError(400, "GPU must be 0–7")
            gpu = int(p["gpu"])
        try:
            vram_gib = float(p.get("vram_gib", "1"))
            minutes = int(p.get("duration_minutes", "60"))
            count = int(p.get("count", "1"))
        except ValueError:
            raise ClaimError(400, "invalid query")
        if not (math.isfinite(vram_gib) and vram_gib > 0) or not 1 <= minutes \
                <= MAX_DURATION_MIN or not 1 <= count <= GPU_COUNT:
            raise ClaimError(400, "invalid query")
        vram_mib = round(vram_gib * MIB_PER_GIB)
        now = self.server.clock()
        rows = list_window_ro(self.server.claims_path, now)
        live = [b for b in rows if b.cancelled_at is None and b.end > now]
        hit = earliest_fit_group(live, count, vram_mib, timedelta(minutes=minutes), cards,
                                 now, gpus=None if gpu is None else [gpu])
        want = cards if gpu is None else {gpu: cards[gpu]}
        free = max((mib - booked_at(live, g, now) for g, mib in want.items()), default=0)
        self._json(200, {"free_mib": max(free, 0),
                         "free_gib": round(max(free, 0) / MIB_PER_GIB, 1),
                         "earliest": None if hit is None else hit[0].isoformat(),
                         "gpu": None if hit is None else hit[1][0],
                         "gpus": [] if hit is None else hit[1]})
```

`ClaimError` is already rendered by `do_GET`'s handler after Task 8's change, so a 400 from
here comes out in the standard shape.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_claims.py -v`
Expected: PASS. Then `python -m pytest -q` — `test_web_static.py` counts the route table in
one test; if it asserts an exact route set, add the two new paths there.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/web.py tests/test_web_claims.py
git commit -m "feat(web): release and capacity routes"
```

---

## Task 10: `max_pending_per_user` — the queue-colonisation guard

**Files:**
- Modify: `resourcemonitor/policy.py`, `deploy/policy.example.toml`, `resourcemonitor/claims.py` (`create`), `resourcemonitor/web.py` (`_max_pending`)
- Test: `tests/test_policy.py` (or `tests/test_host.py`'s policy tests — put them where `load_policy` is already tested), `tests/test_claims_assign.py`, `tests/test_web_claims.py`

**Interfaces:**
- Produces: `Policy.max_pending_per_user: int = 16`, `Policy.max_watchers: int = 64`;
  `ClaimsStore.create(..., max_pending: int | None = None)`.

**Decision recorded.** The user was asked for a number and chose not to pick one, so this
task implements the spec's default: **16, counting not-yet-started rows**, so a four-card
group costs four. Rows are the honest measure of future capacity held. It is one line of
`policy.toml` on each host, so changing it later is a config edit and not a release.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claims_assign.py
def test_a_user_may_not_hold_more_than_max_pending_future_rows(store):
    for _ in range(3):
        _assign(store, max_pending=3, vram_mib=79 * 1024)
    with pytest.raises(ClaimError) as e:
        _assign(store, max_pending=3, vram_mib=79 * 1024)
    assert e.value.status == 409
    assert "3" in e.value.detail                 # the message quotes the real limit
    assert e.value.extra["max_pending_per_user"] == 3
    assert e.value.extra["pending"] == 3


def test_a_started_booking_does_not_count_against_the_limit(store):
    running = _assign(store, max_pending=1, vram_mib=79 * 1024)
    assert running.start == T0                   # starts now, so it is not pending
    _assign(store, max_pending=1, vram_mib=79 * 1024)      # the first future one is fine


def test_the_limit_is_per_user(store):
    _assign(store, max_pending=1, vram_mib=79 * 1024)
    _assign(store, max_pending=1, vram_mib=79 * 1024)
    _assign(store, user="duser", max_pending=1, vram_mib=79 * 1024)


def test_a_group_counts_as_one_row_per_card(store):
    with pytest.raises(ClaimError) as e:
        store.create(user="cwinkelmann", gpu=None, vram_mib=79 * 1024, start=None,
                     end=None, duration=H, note=None, ip="10.0.0.1", now=T0, count=4,
                     max_pending=3, cards={g: CARD for g in range(8)})
    assert e.value.status == 409
```

```python
# tests/test_policy.py (append; GOOD and _write already exist in this file)
def test_max_pending_and_max_watchers_default_when_absent(tmp_path):
    pol = load_policy(_write(tmp_path, GOOD))
    assert pol.max_pending_per_user == 16
    assert pol.max_watchers == 64


def test_max_pending_is_read_from_the_booking_section(tmp_path):
    pol = load_policy(_write(tmp_path, GOOD + "[booking]\nmax_pending_per_user = 4\n"
                                              "max_watchers = 8\n"))
    assert pol.max_pending_per_user == 4
    assert pol.max_watchers == 8


@pytest.mark.parametrize("line", ["max_pending_per_user = 0", "max_pending_per_user = -1",
                                  'max_pending_per_user = "many"', "max_watchers = 0",
                                  "max_watchers = 100000"])
def test_bad_booking_limits_are_caught_at_load_time(tmp_path, line):
    with pytest.raises(ValueError):
        load_policy(_write(tmp_path, GOOD + f"[booking]\n{line}\n"))
```

```python
# tests/test_web_claims.py
def test_the_pending_limit_answers_409_over_http(live, tmp_path):
    # the live fixture's policy.toml is empty, so the default 16 applies; book 16 futures
    _post(live + "/api/claims", _booking(vram_gib=79, start=S, end=E))
    bodies = [_post(live + "/api/claims", _booking(user="cwinkelmann", vram_gib=79,
                                                   start=None, end=None,
                                                   duration_minutes=60))
              for _ in range(17)]
    assert bodies[-1][0] == 409
    assert json.loads(bodies[-1][2])["max_pending_per_user"] == 16
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_policy.py tests/test_claims_assign.py -k "pending or watchers" -v`
Expected: FAIL — `AttributeError: 'Policy' object has no attribute 'max_pending_per_user'`

- [ ] **Step 3: Write the implementation**

In `policy.py`, two fields and one shared validator:

```python
    max_pending_per_user: int = 16   # future bookings one user may hold at once
    max_watchers: int = 64           # parked /events connections, ~2x carrot's slot count
```

```python
def _bounded(raw: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = raw.get("booking", {}).get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"booking.{key} must be a whole number, {lo}..{hi}")
    return v
```

and in `load_policy`'s return: `max_pending_per_user=_bounded(raw, "max_pending_per_user",
16, 1, 10_000)`, `max_watchers=_bounded(raw, "max_watchers", 64, 1, 4_096)`.

In `claims.py`'s `create`, inside the transaction, before the assignment:

```python
                if max_pending is not None:
                    held = conn.execute(
                        "SELECT COUNT(*) FROM claims WHERE user = ? AND cancelled_at IS "
                        "NULL AND start > ?", (user, now.isoformat())).fetchone()[0]
                    if held + count > max_pending:
                        raise ClaimError(409,
                                         f"you already hold {held} bookings that have not "
                                         f"started; the limit is {max_pending}",
                                         {"max_pending_per_user": max_pending,
                                          "pending": held})
```

In `web.py`, beside `_grace()`:

```python
    def _max_pending(self) -> int:
        """booking.max_pending_per_user from policy.toml; the default if the file is
        missing or bad. Without it, 13,000 jobs booking at once own the calendar for a
        fortnight and the 80 GiB request is offered a slot in two weeks."""
        try:
            return load_policy(self.server.policy_path).max_pending_per_user
        except Exception:
            return 16
```

and pass `max_pending=self._max_pending()` from `_create` (the argument named in Task 8).

In `deploy/policy.example.toml`, under `[booking]`:

```toml
# A user may hold at most this many bookings that have not started yet. It counts rows, so
# a 4-GPU group costs 4. Without it, a 13,000-job pipeline books the whole calendar and a
# colleague who needs 80 GiB is offered a slot a fortnight out.
max_pending_per_user = 16

# Parked GET /api/claims/<id>/events connections. Only *running* jobs watch, so this is
# about twice carrot's 32 concurrent slots. Past it the route answers 503 and clients poll.
max_watchers = 64
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_policy.py tests/test_claims_assign.py tests/test_web_claims.py -q`
Expected: PASS. Then `python -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/policy.py resourcemonitor/claims.py resourcemonitor/web.py deploy/policy.example.toml tests/
git commit -m "feat(policy): max_pending_per_user keeps one pipeline from owning the calendar"
```

---

## Task 11: `watchers.py` — the registry of parked long-polls

**Files:**
- Create: `resourcemonitor/watchers.py`
- Test: `tests/test_watchers.py`

**Interfaces:**
- Produces:
```python
class Watch:
    claim_id: int
    def wait(self, timeout: float) -> dict | None   # the event, or None on timeout/shutdown
    def close(self) -> None
    def __enter__(self) -> "Watch"
    def __exit__(self, *exc) -> None

class Watchers:
    def __init__(self, max_watchers: int = 64)
    def register(self, claim_id: int) -> Watch | None   # None when full or shut down
    def attached(self, claim_id: int) -> bool
    def notify(self, claim_id: int, event: dict) -> int  # how many waiters were woken
    def count(self) -> int
    def shutdown(self) -> None
```
`attached()` is what `create()` calls to decide whether a declared notice counts (Task 12).
`notify()` is called by `web.py` **after** the booking transaction commits (Task 13).

- [ ] **Step 1: Write the failing tests**

```python
"""The in-memory registry behind GET /api/claims/<id>/events. Threads, no HTTP, no DB."""
import threading
import time

from resourcemonitor.watchers import Watchers


def test_nothing_is_attached_to_begin_with():
    w = Watchers()
    assert w.attached(1) is False
    assert w.count() == 0


def test_registering_attaches_and_closing_detaches():
    w = Watchers()
    watch = w.register(7)
    assert watch is not None
    assert w.attached(7) is True and w.count() == 1
    watch.close()
    assert w.attached(7) is False and w.count() == 0


def test_the_context_manager_detaches_even_on_an_exception():
    w = Watchers()
    try:
        with w.register(7):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert w.attached(7) is False


def test_a_waiter_is_woken_with_the_event():
    w = Watchers()
    got = []
    watch = w.register(7)

    def reader():
        got.append(watch.wait(timeout=5))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    assert w.notify(7, {"event": "taken"}) == 1
    t.join(5)
    assert got == [{"event": "taken"}]


def test_an_event_that_arrives_before_the_wait_is_not_lost():
    w = Watchers()
    watch = w.register(7)
    w.notify(7, {"event": "taken"})
    assert watch.wait(timeout=0.1) == {"event": "taken"}


def test_waiting_times_out_with_none():
    w = Watchers()
    watch = w.register(7)
    assert watch.wait(timeout=0.05) is None


def test_notifying_a_booking_nobody_watches_is_harmless():
    assert Watchers().notify(7, {"event": "taken"}) == 0


def test_two_watchers_on_one_booking_are_both_woken():
    w = Watchers()
    a, b = w.register(7), w.register(7)
    assert w.notify(7, {"event": "taken"}) == 2
    assert a.wait(timeout=0.1) == b.wait(timeout=0.1) == {"event": "taken"}


def test_the_cap_refuses_the_next_registration():
    w = Watchers(max_watchers=2)
    assert w.register(1) is not None
    assert w.register(2) is not None
    assert w.register(3) is None
    assert w.count() == 2


def test_a_closed_watch_frees_a_slot():
    w = Watchers(max_watchers=1)
    first = w.register(1)
    assert w.register(2) is None
    first.close()
    assert w.register(2) is not None


def test_shutdown_wakes_everyone_and_refuses_new_registrations():
    w = Watchers()
    watch = w.register(7)
    done = threading.Event()

    def reader():
        assert watch.wait(timeout=5) is None
        done.set()

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    w.shutdown()
    assert done.wait(5) is True
    assert w.register(8) is None
    t.join(5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_watchers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'resourcemonitor.watchers'`

- [ ] **Step 3: Write the implementation**

```python
"""Who is listening for news about a booking.

A job that holds a card keeps one GET /api/claims/<id>/events parked here, so the server
can tell it in milliseconds that its card has been taken instead of the job polling. The
parked connection is also the only liveness signal there is: when the job dies, TCP drops
and the watch disappears, which is how claims.create() knows whether a declared notice is
worth honouring.

This module knows nothing about HTTP, bookings or SQLite -- only integer ids and dicts.

Lock order, and the whole reason this is a separate module: callers must never hold this
lock while taking the ClaimsStore lock. Events are collected inside the booking
transaction and delivered by web.py *after* COMMIT."""
from __future__ import annotations

import threading


class Watch:
    """One parked connection. Not created directly: see Watchers.register."""

    def __init__(self, registry: "Watchers", claim_id: int):
        self._registry = registry
        self.claim_id = claim_id
        self.event: dict | None = None
        self.closed = False

    def wait(self, timeout: float) -> dict | None:
        """Block until an event arrives, the timeout passes, or the server shuts down."""
        cond = self._registry._cond
        with cond:
            cond.wait_for(lambda: self.event is not None or self.closed
                          or self._registry._down, timeout)
            return self.event

    def close(self) -> None:
        self._registry._drop(self)

    def __enter__(self) -> "Watch":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Watchers:
    def __init__(self, max_watchers: int = 64):
        self.max_watchers = max_watchers
        self._cond = threading.Condition()
        self._by_claim: dict[int, list[Watch]] = {}
        self._down = False

    def register(self, claim_id: int) -> Watch | None:
        """A Watch, or None when the cap is reached (the route then answers 503 and the
        client falls back to polling) or the server is shutting down."""
        with self._cond:
            if self._down or self._count() >= self.max_watchers:
                return None
            watch = Watch(self, claim_id)
            self._by_claim.setdefault(claim_id, []).append(watch)
            return watch

    def attached(self, claim_id: int) -> bool:
        with self._cond:
            return bool(self._by_claim.get(claim_id))

    def notify(self, claim_id: int, event: dict) -> int:
        """Hand `event` to every watcher of this booking and wake them. Returns how many
        there were, which is 0 for a job that is not listening -- not an error."""
        with self._cond:
            watches = list(self._by_claim.get(claim_id, ()))
            for w in watches:
                w.event = event
            if watches:
                self._cond.notify_all()
            return len(watches)

    def count(self) -> int:
        with self._cond:
            return self._count()

    def shutdown(self) -> None:
        """Wake every waiter with nothing, so server_close() is never held up by a parked
        request, and refuse further registrations."""
        with self._cond:
            self._down = True
            self._by_claim.clear()
            self._cond.notify_all()

    def _count(self) -> int:
        return sum(len(v) for v in self._by_claim.values())

    def _drop(self, watch: Watch) -> None:
        with self._cond:
            watch.closed = True
            rest = [w for w in self._by_claim.get(watch.claim_id, ()) if w is not watch]
            if rest:
                self._by_claim[watch.claim_id] = rest
            else:
                self._by_claim.pop(watch.claim_id, None)
            self._cond.notify_all()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_watchers.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/watchers.py tests/test_watchers.py
git commit -m "feat(watchers): registry of parked long-polls, with a cap and a shutdown"
```

---

## Task 12: `notice_seconds` — the victim declares its warning, and it may only shorten

**Files:**
- Modify: `resourcemonitor/claims.py` — `create` (`notice_seconds`, `watching`, the grace computation)
- Test: `tests/test_claims_revoke.py`

**Interfaces:**
- Consumes: `Watchers.attached` (Task 11), passed in as `watching`.
- Produces: `create(..., notice_seconds: int | None = None, watching: Callable[[int], bool] = lambda _id: False)`.
  The new booking's own `notice_seconds` is stored on its row; `watching` is consulted about
  its **victims**.

**Review Focus item 1 lives here.** `effective = min(declared, grace)`: a declared notice may
only ever *shorten* the wait, never lengthen it. Without the clamp a job declaring
`notice_seconds = 86400` becomes unpreemptable for a day, which inverts the whole feature.

- [ ] **Step 1: Write the failing tests**

```python
"""Notice: how long an important booking waits before it takes a running lendable card."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import DEFAULT_CARD_MIB, GPU_COUNT, ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers

H = timedelta(hours=1)
CARDS = {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}
GRACE = timedelta(minutes=30)


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _lendable(store, *, notice=None, gib=79, hours=4, now=T0):
    return store.create(user="cwinkelmann", gpu=4, vram_mib=gib * 1024, start=now,
                        end=now + timedelta(hours=hours), note=None, ip="10.0.0.1",
                        now=now, priority="lendable", notice_seconds=notice,
                        cards=CARDS).booking


def _important(store, *, now, watching=lambda _id: False, gib=79, hours=2):
    return store.create(user="duser", gpu=4, vram_mib=gib * 1024, start=now,
                        end=now + timedelta(hours=hours), note=None, ip="10.0.0.2",
                        now=now, priority="important", grace=GRACE, watching=watching,
                        cards=CARDS)


def test_notice_seconds_is_stored_on_the_row(store):
    b = _lendable(store, notice=0)
    assert store.get(b.id).notice_seconds == 0


def test_an_unwatched_victim_keeps_the_full_grace(store):
    _lendable(store, notice=0)                       # declared, but nobody is listening
    made = _important(store, now=T0 + timedelta(minutes=5))
    assert made.booking.start == T0 + timedelta(minutes=35)


def test_a_victim_with_no_declared_notice_keeps_the_full_grace(store):
    victim = _lendable(store, notice=None)
    made = _important(store, now=T0 + timedelta(minutes=5),
                      watching=lambda i: i == victim.id)
    assert made.booking.start == T0 + timedelta(minutes=35)


def test_a_watched_victim_declaring_zero_is_taken_at_once(store):
    victim = _lendable(store, notice=0)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now
    assert made.adjusted_start is None


def test_a_watched_victim_declaring_two_minutes_gets_two_minutes(store):
    victim = _lendable(store, notice=120)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now + timedelta(minutes=2)


def test_a_declared_notice_may_only_shorten_never_lengthen(store):
    victim = _lendable(store, notice=86400)          # "give me a day"
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now + GRACE         # clamped to grace_minutes


def test_the_longest_notice_among_several_victims_wins(store):
    quick = _lendable(store, gib=40, notice=0)
    slow = store.create(user="auser", gpu=4, vram_mib=39 * 1024, start=T0,
                        end=T0 + timedelta(hours=4), note=None, ip="10.0.0.3", now=T0,
                        priority="lendable", notice_seconds=300, cards=CARDS).booking
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=79,
                      watching=lambda i: i in (quick.id, slow.id))
    assert made.booking.start == now + timedelta(minutes=5)


def test_a_future_victim_is_not_running_so_there_is_no_notice_at_all(store):
    store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024,
                 start=T0 + timedelta(hours=2), end=T0 + timedelta(hours=4), note=None,
                 ip="10.0.0.1", now=T0, priority="lendable", notice_seconds=None,
                 cards=CARDS)
    made = _important(store, now=T0, hours=1)
    assert made.booking.start == T0           # nothing is running; grace does not apply


def test_grace_zero_in_policy_still_means_instant(store):
    victim = _lendable(store, notice=None)
    made = store.create(user="duser", gpu=4, vram_mib=79 * 1024,
                        start=T0 + timedelta(minutes=5),
                        end=T0 + timedelta(hours=2), note=None, ip="10.0.0.2",
                        now=T0 + timedelta(minutes=5), priority="important",
                        grace=timedelta(0), watching=lambda i: i == victim.id, cards=CARDS)
    assert made.booking.start == T0 + timedelta(minutes=5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims_revoke.py -v`
Expected: FAIL — `TypeError: create() got an unexpected keyword argument 'notice_seconds'`

- [ ] **Step 3: Write the implementation**

Add to `create`'s signature:

```python
               notice_seconds: int | None = None,
               watching: Callable[[int], bool] = lambda _id: False,
```

with `from typing import Callable, Protocol` at the top, and validate it with the others:

```python
        if notice_seconds is not None and (not isinstance(notice_seconds, int)
                                           or isinstance(notice_seconds, bool)
                                           or notice_seconds < 0):
            raise ClaimError(400, "notice_seconds must be 0 or more")
```

Replace the grace block inside the transaction:

```python
                running = {b.id: b for b in overlapping if b.start <= now}
                victims = {t["id"] for t in takes if t["id"] in running}
                if victims:
                    def notice_of(b: Booking) -> timedelta:
                        """A victim that is being listened to may shorten the wait to what
                        it declared; it can never lengthen it past the policy grace, or a
                        job could make itself unpreemptable by asking for a day."""
                        if b.notice_seconds is None or not watching(b.id):
                            return grace
                        return min(timedelta(seconds=b.notice_seconds), grace)

                    notice = max(notice_of(running[i]) for i in victims)
                    if notice > timedelta(0) and any(
                            t["start"] < now + notice for t in takes if t["id"] in victims):
                        start = now + notice          # rounded up to the whole minute
                        if start.second or start.microsecond:
                            start = start.replace(second=0, microsecond=0) \
                                + timedelta(minutes=1)
                        if end <= start:
                            raise ClaimError(409, "this takes from a lendable booking that "
                                                  "is already running, so it must last "
                                                  "beyond "
                                                  f"{int(notice.total_seconds() // 60)} "
                                                  "minutes from now")
                        takes = takes_of(draft(start), overlapping, card_mib)
```

and add `notice_seconds` to the INSERT column list and values, in both the single and the
group branch (`int(notice_seconds)` or `None`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims_revoke.py -v`
Expected: PASS, 9 tests. Then `python -m pytest -q` — `test_claims_priority.py` covers
today's 30-minute grace with no watcher and must stay green, which is the regression that
matters here.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims_revoke.py
git commit -m "feat(claims): a watched victim may shorten its own notice, never lengthen it"
```

---

## Task 13: Revocation — the starved victim's row is truncated

**Files:**
- Modify: `resourcemonitor/claims.py` — `create` (the revocation block, after the INSERT)
- Test: `tests/test_claims_revoke.py`

**Interfaces:**
- Consumes: `allocate` (already in `claims.py`), `_advance` (Task 7).
- Produces: `Created.revoked: list[dict]`, each
  `{"id", "user", "gpu", "effective_mib", "lost_mib", "at", "by", "truncated"}` with `at`
  an ISO string and `by` a list of one user name. `web.py` turns each entry into a
  notification (Task 14); nothing else reads it.

**Review Focus item 4 lives here.** `end` can never fall below `start`, because only a
booking that is `active_at(at)` — `start <= at < end` — is ever truncated, and it is
truncated to `at`. The `UPDATE … WHERE end > ?` guard makes a repeated revocation a no-op.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_claims_revoke.py
def test_a_starved_victim_is_truncated_at_the_instant_it_loses_the_card(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=79, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == now
    assert store.get(victim.id).cancelled_at is None       # it ran; energy still counts
    assert [r["id"] for r in made.revoked] == [victim.id]
    assert made.revoked[0]["truncated"] is True
    assert made.revoked[0]["effective_mib"] == 0
    assert made.revoked[0]["by"] == ["duser"]


def test_a_victim_shrunk_but_not_starved_is_reported_and_left_alone(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=40, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == T0 + timedelta(hours=4)
    assert made.revoked[0]["truncated"] is False
    assert made.revoked[0]["effective_mib"] > 0
    assert made.revoked[0]["lost_mib"] > 0


def test_truncation_waits_for_the_notice_rather_than_happening_now(store):
    victim = _lendable(store, notice=120, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == now + timedelta(minutes=2)


def test_an_important_booking_never_revokes_another_important_one(store):
    first = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=T0,
                         end=T0 + timedelta(hours=4), note=None, ip="10.0.0.1", now=T0,
                         priority="important", cards=CARDS).booking
    with pytest.raises(ClaimError) as e:
        _important(store, now=T0 + timedelta(minutes=5), gib=79)
    assert e.value.status == 409
    assert store.get(first.id).end == T0 + timedelta(hours=4)


def test_a_lendable_booking_revokes_nothing(store):
    victim = _lendable(store, notice=0, gib=40, hours=4)
    made = store.create(user="duser", gpu=4, vram_mib=39 * 1024, start=T0,
                        end=T0 + H, note=None, ip="10.0.0.2", now=T0,
                        priority="lendable", cards=CARDS)
    assert made.revoked == []
    assert store.get(victim.id).end == T0 + timedelta(hours=4)


def test_revoking_twice_at_the_same_instant_truncates_once(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id, hours=1)
    second = store.create(user="auser", gpu=4, vram_mib=79 * 1024, start=now + H,
                          end=now + timedelta(hours=2), note=None, ip="10.0.0.3", now=now,
                          priority="important", watching=lambda i: i == victim.id,
                          cards=CARDS)
    assert store.get(victim.id).end == now            # unchanged by the second booking
    assert second.revoked == []


def test_truncation_never_leaves_an_end_before_the_start(store):
    victim = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024,
                          start=T0 + timedelta(minutes=10),
                          end=T0 + timedelta(hours=4), note=None, ip="10.0.0.1", now=T0,
                          priority="lendable", notice_seconds=0, cards=CARDS).booking
    _important(store, now=T0, gib=79, hours=3, watching=lambda i: i == victim.id)
    got = store.get(victim.id)
    assert got.end >= got.start


def test_the_freed_tail_advances_someone_else(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    waiting = store.create(user="auser", gpu=4, vram_mib=79 * 1024, start=None,
                           end=None, duration=H, note=None, ip="10.0.0.3", now=T0,
                           auto_advance=True, cards=CARDS).booking
    assert waiting.start == T0 + timedelta(hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id, hours=1)
    # the victim ends at `now`, so the important hour runs now..+1h and the waiting slot
    # moves up behind it instead of waiting out the cancelled three hours.
    assert store.get(waiting.id).start == now + H
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claims_revoke.py -k "starved or shrunk or revok or freed_tail" -v`
Expected: FAIL — `Created.revoked` exists from Task 4 but is always empty, so the failure is
`assert [] == [1]`, and the victim's `end` is still four hours out.

- [ ] **Step 3: Write the implementation**

In `create`, after the INSERT and before `COMMIT`:

```python
                revoked = self._revoke(conn, booking_row=Booking(
                    id=claim_id, user=user, gpu=gpu, vram_mib=vram_mib, start=start,
                    end=end, note=note, created_at=now, created_ip=ip, priority=priority),
                    overlapping=overlapping, card_mib=card_mib, now=now, cards=cards)
```

```python
    def _revoke(self, conn, *, booking_row: Booking, overlapping: list[Booking],
                card_mib: int, now: datetime, cards: dict[int, int]) -> list[dict]:
        """Write down what this important booking takes. allocate() already decided it:
        important bookings get their full VRAM and lendable ones split what is left,
        oldest id first, so the newest lendable booking is the one that starves. A victim
        left with nothing has its end truncated to the instant it loses the card -- the
        window it really used stays on the calendar, exactly as an early release. A victim
        merely shrunk keeps its row and is only told.

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
            truncated = False
            if effective == 0:
                cur = conn.execute("UPDATE claims SET end = ? WHERE id = ? AND end > ?",
                                   (at.isoformat(), v.id, at.isoformat()))
                truncated = cur.rowcount > 0
            out.append({"id": v.id, "user": v.user, "gpu": v.gpu,
                        "effective_mib": effective, "lost_mib": v.vram_mib - effective,
                        "at": at.isoformat(), "by": [booking_row.user],
                        "truncated": truncated})
        if any(r["truncated"] for r in out):
            self._advance(conn, now, cards)
        return out
```

Return `Created([booking], takes, asked if start != asked else None, assigned_later,
revoked)`.

`_revoke` takes `conn` and is called only from inside `create`'s transaction, so the
truncation, the new booking and any advance commit together or not at all.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claims_revoke.py -v`
Expected: PASS, 17 tests. Then `python -m pytest -q` — `test_claims_priority.py`'s
`taken_segments` tests still pass: nothing about `allocate()` changed, only whether the
result is written down.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py tests/test_claims_revoke.py
git commit -m "feat(claims): a starved lendable booking is truncated, not cancelled"
```

---

## Task 14: `GET /api/claims/{id}/events` — the interrupt

**Files:**
- Modify: `resourcemonitor/claims.py` (`get_ro`), `resourcemonitor/web.py` (route, `_events`, `_Server.watchers`, `server_close`, notify after a create)
- Test: `tests/test_web_events.py`

**Interfaces:**
- Consumes: `Watchers` (Task 11), `Created.revoked` (Task 13).
- Produces:
```python
# claims.py
def get_ro(path: Path | str, claim_id: int) -> Booking | None   # mode=ro, fail-open
# web.py
EVENT_HOLD_S = 60
_Server.watchers: Watchers
```
```
GET /api/claims/{id}/events
  200 {"event":"taken","effective_mib":0,"lost_mib":80896,"by":["duser"],
       "deadline":"…","truncated":true}
  204 (held EVENT_HOLD_S, nothing happened — reconnect)
  404 unknown booking · 409 cancelled or already over · 503 past max_watchers
```

**Review Focus items 3 and 5 live here.** A watch on a cancelled, finished or unknown
booking answers at once and parks nothing. `server_close()` calls `watchers.shutdown()`
first, so a parked request cannot delay the restart the user performs by hand on a deploy.

- [ ] **Step 1: Write the failing tests**

```python
"""The long-poll a running job dials out on. Real HTTP, real threads."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from resourcemonitor import web
from resourcemonitor.web import make_server
from tests.claims_fixture import T0, FakeUsers
from tests.history_fixture import build_history

HOLD = 1.0


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    path = tmp_path_factory.mktemp("hist") / "h.sqlite"
    build_history(path, T0 - timedelta(hours=2), hours=2)
    return path


@pytest.fixture(autouse=True)
def fast_hold(monkeypatch):
    """A 60 s hold is right in production and unusable in a test."""
    monkeypatch.setattr(web, "EVENT_HOLD_S", HOLD)
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter())


@pytest.fixture
def live(tmp_path, history):
    pol = tmp_path / "policy.toml"
    pol.write_text("")
    srv = make_server("127.0.0.1", 0, history, pol, stale_after_s=600, clock=lambda: T0,
                      claims_path=tmp_path / "claims.sqlite", users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _book(base, **over):
    d = {"user": "cwinkelmann", "gpu": 4, "vram_gib": 79, "start": T0.isoformat(),
         "end": (T0 + timedelta(hours=4)).isoformat(), "priority": "lendable",
         "notice_seconds": 0}
    d.update(over)
    return _post(base + "/api/claims", d)[1]["claim"]


def test_an_unknown_booking_is_404_and_parks_nothing(live):
    srv, base = live
    assert _get(base + "/api/claims/999/events")[0] == 404
    assert srv.watchers.count() == 0


def test_a_cancelled_booking_is_409(live):
    srv, base = live
    claim = _book(base)
    _post(f"{base}/api/claims/{claim['id']}/cancel", {})
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 409
    assert srv.watchers.count() == 0


def test_a_booking_already_over_is_409(live):
    srv, base = live
    claim = _book(base, start=(T0 - timedelta(hours=2)).isoformat(),
                  end=(T0 - timedelta(hours=1)).isoformat())
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 409


def test_nothing_happening_answers_204_after_the_hold(live):
    srv, base = live
    claim = _book(base)
    began = time.monotonic()
    status, body = _get(f"{base}/api/claims/{claim['id']}/events")
    assert status == 204 and body is None
    assert HOLD <= time.monotonic() - began < HOLD + 5
    assert srv.watchers.count() == 0


def test_the_watcher_is_woken_when_an_important_booking_takes_the_card(live):
    srv, base = live
    victim = _book(base)
    got = []

    def watch():
        got.append(_get(f"{base}/api/claims/{victim['id']}/events", timeout=20))

    t = threading.Thread(target=watch)
    t.start()
    for _ in range(100):                       # wait for the connection to be parked
        if srv.watchers.attached(victim["id"]):
            break
        time.sleep(0.02)
    assert srv.watchers.attached(victim["id"]) is True
    status, body = _post(base + "/api/claims",
                         {"user": "duser", "gpu": 4, "vram_gib": 79,
                          "start": T0.isoformat(),
                          "end": (T0 + timedelta(hours=1)).isoformat(),
                          "priority": "important"})
    assert status == 201
    assert [r["id"] for r in body["revoked"]] == [victim["id"]]
    t.join(20)
    assert got[0][0] == 200
    assert got[0][1]["event"] == "taken"
    assert got[0][1]["effective_mib"] == 0
    assert got[0][1]["by"] == ["duser"]


def test_past_max_watchers_the_route_answers_503(live, monkeypatch):
    srv, base = live
    claim = _book(base)
    srv.watchers.max_watchers = 0
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 503


def test_a_parked_request_does_not_delay_shutdown(tmp_path, history):
    pol = tmp_path / "policy.toml"
    pol.write_text("")
    srv = make_server("127.0.0.1", 0, history, pol, stale_after_s=600, clock=lambda: T0,
                      claims_path=tmp_path / "claims.sqlite", users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    claim = _book(base)
    threading.Thread(target=lambda: _get(f"{base}/api/claims/{claim['id']}/events",
                                         timeout=20), daemon=True).start()
    for _ in range(100):
        if srv.watchers.attached(claim["id"]):
            break
        time.sleep(0.02)
    began = time.monotonic()
    srv.shutdown()
    srv.server_close()
    assert time.monotonic() - began < 5


def test_posting_to_the_events_route_is_not_a_write(live):
    srv, base = live
    claim = _book(base)
    assert _post(f"{base}/api/claims/{claim['id']}/events", {})[0] in (404, 405)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_events.py -v`
Expected: FAIL — `404 {"error": "not found"}` for every events request, and
`AttributeError: '_Server' object has no attribute 'watchers'`

- [ ] **Step 3: Write the implementation**

In `claims.py`, beside `load_active`:

```python
def get_ro(path: Path | str, claim_id: int) -> Booking | None:
    """One booking, read with mode=ro (fail-open, see _read_ro). GETs never open the
    bookings file for writing."""
    if not isinstance(claim_id, int):
        return None
    rows = _read_ro(path, "WHERE id = ? LIMIT 1", (claim_id,))
    return rows[0] if rows else None
```

In `web.py`:

```python
from resourcemonitor.watchers import Watchers

_EVENTS = re.compile(r"^/api/claims/(\d{1,9})/events$", re.ASCII)
EVENT_HOLD_S = 60       # how long one request is parked before the client reconnects
```

In `do_GET`, before the handler-map lookup:

```python
            m_events = _EVENTS.match(path)
            if m_events is not None:
                self._events(int(m_events.group(1)))
                return
```

The events route is deliberately **not** in `_HEAVY`: those two semaphore slots exist to
stop SQLite scans piling up, and a parked connection holding one would block the dashboard.

```python
    def _events(self, claim_id: int) -> None:
        """Park this request until the booking is squeezed, then answer with one event.
        The job dials out because nothing can dial in: the t14 has no route to carrot's
        subnet. The parked connection is also the liveness signal claims.create() reads
        through Watchers.attached, which is why a dead job stops being promised notice."""
        now = self.server.clock()
        b = get_ro(self.server.claims_path, claim_id)
        if b is None:
            self._json(404, {"error": "not found"})
            return
        if b.cancelled_at is not None:
            self._json(409, {"error": "conflict", "detail": "already cancelled"})
            return
        if b.end <= now:
            self._json(409, {"error": "conflict", "detail": "booking is over"})
            return
        watch = self.server.watchers.register(claim_id)
        if watch is None:
            self._json(503, {"error": "busy"})
            return
        with watch:
            event = watch.wait(EVENT_HOLD_S)
        if event is None:
            self._send(204, b"", "application/json")
        else:
            self._json(200, event)
```

Add `get_ro` to the `resourcemonitor.claims` import list at the top of `web.py`.

Notify after the transaction has committed — the create path, once `self._create(body)` has
returned:

```python
                made = self._create(body)
                for r in made.revoked:
                    self.server.watchers.notify(r["id"], {
                        "event": "taken", "effective_mib": r["effective_mib"],
                        "lost_mib": r["lost_mib"], "by": r["by"],
                        "truncated": r["truncated"], "deadline": r["at"]})
```

This is the only place `notify` is called, and it is outside the store's lock by
construction: `_create` has returned, so the transaction is committed and the `ClaimsStore`
lock released. Never move this inside `claims.py`.

In `_Server`:

```python
    watchers: Watchers

    def server_close(self) -> None:
        self.watchers.shutdown()        # before super(), so no request is still parked
        super().server_close()
        with self.store_lock:
            if self.store is not None:
                self.store.close()
                self.store = None
```

In `make_server`, after `srv.users = …`:

```python
    try:
        cap = load_policy(Path(policy_path)).max_watchers
    except Exception:
        cap = 64
    # A thread budget, not a policy decision: changing it needs a restart, like the port.
    srv.watchers = Watchers(max_watchers=cap)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_events.py -v`
Expected: PASS, 8 tests. Then `python -m pytest -q` — the whole suite, including
`test_web.py`'s route coverage.

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/claims.py resourcemonitor/web.py tests/test_web_events.py
git commit -m "feat(web): GET /api/claims/<id>/events parks until the card is taken"
```

---

## Task 15: Dashboard — book the earliest slot, and show when a slot is assigned

**Files:**
- Modify: `resourcemonitor/web/index.html:43-70` (the form), `resourcemonitor/web/app.js` (a new top-level `bookingState`, the form's submit payload, the booking list badge), `resourcemonitor/web/app.css`
- Test: `tests/test_web_static.py`

**Interfaces:**
- Consumes: the `POST /api/claims` contract from Task 8 and `assigned_later` in its reply.
- Produces: `function bookingState(claim, nowMs)` at the top level of `app.js`, returning
  `"cancelled" | "over" | "running" | "assigned"`.

**Already covered, do not rebuild.** `GET /api/claims` attaches a `taken` array to every
lendable booking (`web.py:417`, from `taken_segments`), and the page already renders it. A
revoked booking therefore already shows who took the card and when — revocation needs no new
display, only the shorter `end` the server now writes.

**Out of scope, recorded:** `app.js` carries its own `allocate`, `freeVramMiB` and
`takesPreview` — a client-side copy of the server's capacity arithmetic, and exactly the
duplication `GET /api/capacity` exists to end. Replacing them with a call to the new route is
a worthwhile follow-up with its own plan; it is not in this one, because it rewrites the
form's live preview and its node-based tests.

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_web_static.py
def _node_top_level(name, call):
    """Extract a column-0 helper from app.js and evaluate `call` with node (skip without).
    Mirrors the existing extractor for the indented helpers."""
    import json, shutil, subprocess
    js = (WEB / "app.js").read_text()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    m = re.search(r"^function " + name + r"\(.*?^\}$", js, re.S | re.M)
    assert m is not None, name
    out = subprocess.run([node, "-e", m.group(0) + "\nconsole.log(JSON.stringify("
                          + call + "));"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_booking_state_names_the_four_states():
    now = 1000000
    assert _node_top_level("bookingState",
                           "bookingState({cancelled_at:'x',start:'',end:''}, 0)") \
        == "cancelled"
    assert _node_top_level(
        "bookingState",
        f"bookingState({{cancelled_at:null,start:new Date({now - 2000}).toISOString(),"
        f"end:new Date({now - 1000}).toISOString()}}, {now})") == "over"
    assert _node_top_level(
        "bookingState",
        f"bookingState({{cancelled_at:null,start:new Date({now - 1000}).toISOString(),"
        f"end:new Date({now + 1000}).toISOString()}}, {now})") == "running"
    assert _node_top_level(
        "bookingState",
        f"bookingState({{cancelled_at:null,start:new Date({now + 1000}).toISOString(),"
        f"end:new Date({now + 2000}).toISOString()}}, {now})") == "assigned"


def test_the_form_can_ask_for_the_earliest_slot():
    html = (WEB / "index.html").read_text()
    for id_ in ("book-earliest", "book-duration", "book-advance"):
        assert f'id="{id_}"' in html, id_


def test_the_page_still_has_no_inline_script_or_external_url():
    html = (WEB / "index.html").read_text()
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None
    assert " style=" not in html
    for f in ("index.html", "app.js", "app.css"):
        assert re.search(r"https?://", (WEB / f).read_text()) is None, f
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_static.py -k "booking_state or earliest_slot" -v`
Expected: FAIL — `assert None is not None, 'bookingState'`, then `assert 'id="book-earliest"' in html`

- [ ] **Step 3: Write the implementation**

In `app.js`, at the top level beside `allocate`:

```javascript
// Which of the four states a booking is in, for the badge in the bookings list. An
// "assigned" booking has not started: the server chose its window, and an early release
// by someone else may still move it earlier.
function bookingState(claim, nowMs) {
  if (claim.cancelled_at) return "cancelled";
  const start = Date.parse(claim.start), end = Date.parse(claim.end);
  if (end <= nowMs) return "over";
  if (start <= nowMs) return "running";
  return "assigned";
}
```

In `index.html`, inside `#book-form`, after the `until` row:

```html
        <span class="label">when</span>
        <label class="check" for="book-earliest"><input id="book-earliest" name="earliest" type="checkbox"> Earliest slot</label>
        <label for="book-duration">for (minutes)</label>
        <input id="book-duration" name="duration_minutes" type="number" min="1" max="20160" step="1" placeholder="60" inputmode="numeric">
        <label class="check" for="book-advance"><input id="book-advance" name="auto_advance" type="checkbox"> Move earlier if a card frees up</label>
```

In the form's submit handler, where the body is built today from `book-from` and the until
controls, branch on the checkbox — when "Earliest slot" is ticked, send neither `start` nor
`end`:

```javascript
    const earliest = form.elements["earliest"].checked;
    if (earliest) {
      delete body.start;
      delete body.end;
      body.duration_minutes = Number(form.elements["duration_minutes"].value || 60);
    }
    if (form.elements["auto_advance"].checked) body.auto_advance = true;
```

and make `book-from` not `required` while the checkbox is ticked (set `.required = !earliest`
in the checkbox's `change` listener), or the browser blocks submission.

In the bookings list, where each row's text is assembled, append the badge with
`textContent` (never `innerHTML` — `tests/test_web_static.py` forbids it):

```javascript
      const state = bookingState(b, Date.now());
      if (state === "assigned") row.appendChild(el("span", "badge assigned", "assigned"));
```

In `app.css`, one rule beside the existing badge rules:

```css
.badge.assigned { background: #2b3a55; color: #cfe3ff; }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_static.py -v`
Expected: PASS (the `bookingState` tests skip where node is missing — that is the suite's
existing single skip). Then `python -m pytest -q`.

Manual check, because a form is not fully testable from pytest: start the server against a
scratch claims file, tick "Earliest slot", book 60 minutes, and confirm the row appears with
the `assigned` badge and the calendar draws it at the assigned time.

```bash
python -m resourcemonitor serve --bind 127.0.0.1 --port 8799 \
  --claims /tmp/acceptance-claims.sqlite --policy deploy/policy.example.toml
```

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/web/index.html resourcemonitor/web/app.js resourcemonitor/web/app.css tests/test_web_static.py
git commit -m "feat(web): book the earliest slot from the form, badge an assigned one"
```

---

## Task 16: t14 acceptance run

**Files:**
- Create: `deploy/acceptance-slots.py`
- Modify: `README.md`, `.claude/skills/deploy-resourcemonitor/SKILL.md`
- Test: `tests/test_skills_present.py` (the script's shape), plus the run itself on t14

**Interfaces:**
- Consumes: every route from Tasks 8, 9 and 14.
- Produces: the executable HTTP contract the `hrm` SDK is implemented against, in its own
  repo. Run as:
```bash
python3 deploy/acceptance-slots.py --base http://192.168.188.88:8765 --user christian --gib 6
```

**Why this script exists.** The SDK lives in another repo, so without it nothing in this one
exercises book → wait → revoke → release end to end. It is also the only way to check the
t14 deployment, where the card is 16376 MiB rather than carrot's 81559.

**Prerequisite that will bite.** `_cards()` falls back to eight phantom 81559 MiB cards when
the history file has no poll yet. On t14 that would let the script book 79 GiB on a 16 GiB
card and prove nothing. So `watch` must have polled at least once before the run, and the
script refuses to continue if `/api/capacity` reports a card larger than `--gib * 3`.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_skills_present.py
def test_the_acceptance_script_is_stdlib_only_and_never_posts_to_slack():
    src = Path("deploy/acceptance-slots.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("requests", "SLACK", "webhook", "pynvml"):
        assert banned not in code, banned
    assert "urllib.request" in code
    assert "/api/claims" in code and "/events" in code and "/release" in code
    assert "/api/capacity" in code
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_skills_present.py -k acceptance -v`
Expected: FAIL — `FileNotFoundError: deploy/acceptance-slots.py`

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""End-to-end acceptance run for assigned slots and revocation, against a live server.

Stdlib only, no GPU work: bookings are bookkeeping, so this proves the booking server
without allocating a byte of VRAM. It plays the part the hrm SDK plays in production --
book, park a watcher, get taken, release -- and is the contract the SDK is written against.

    python3 deploy/acceptance-slots.py --base http://192.168.188.88:8765 \
        --user christian --gib 6

Each step prints PASS or FAIL; the exit status is non-zero if any step failed. Every
booking it makes is released or cancelled before it exits, including on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

TIMEOUT = 15


def call(method: str, url: str, body: dict | None = None, timeout: int = TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


class Run:
    def __init__(self, base: str, user: str, gib: int):
        self.base, self.user, self.gib = base.rstrip("/"), user, gib
        self.failures = 0
        self.made: list[int] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
        if not ok:
            self.failures += 1
        return ok

    def book(self, **body) -> dict | None:
        d = {"user": self.user, "vram_gib": self.gib, "duration_minutes": 10}
        d.update(body)
        status, out = call("POST", self.base + "/api/claims", d)
        if status != 201:
            print(f"      booking refused: {status} {out}")
            return None
        for c in out["claims"]:
            self.made.append(c["id"])
        return out

    def cleanup(self) -> None:
        """Release what started and cancel what did not, so a failed run leaves the box
        as it found it. Both calls are tried for every booking; one of them is a no-op."""
        for cid in reversed(self.made):
            call("POST", f"{self.base}/api/claims/{cid}/release", {})
            call("POST", f"{self.base}/api/claims/{cid}/cancel", {})


    def capacity(self, **q) -> dict:
        query = "&".join(f"{k}={v}" for k, v in q.items())
        status, out = call("GET", f"{self.base}/api/capacity?{query}")
        return out if status == 200 else {}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, help="e.g. http://192.168.188.88:8765")
    p.add_argument("--user", required=True, help="an OS account on that host")
    p.add_argument("--gib", type=int, default=6,
                   help="VRAM per booking; three must not fit on one card")
    args = p.parse_args(argv)
    r = Run(args.base, args.user, args.gib)

    try:
        # 0. the server is there, and it knows the real card size
        status, health = call("GET", r.base + "/healthz")
        r.check("healthz answers", status == 200, str(health))
        cap = r.capacity(vram_gib=1, duration_minutes=10)
        free = cap.get("free_mib", 0)
        if not r.check("capacity reports a plausible card", 0 < free < args.gib * 3 * 1024,
                       f"free_mib={free}; is `watch` running? without a poll the server "
                       f"assumes 81559 MiB cards"):
            return 1

        # 1. a booking with no start begins now
        first = r.book(notice_seconds=0, priority="lendable")
        if not r.check("a free card is assigned now", first is not None
                       and first["assigned_later"] is False):
            return 1
        a = first["claim"]

        # 2. a second fits beside it; a third does not and is assigned later
        second = r.book(notice_seconds=0, priority="lendable")
        r.check("a second booking fits beside the first", second is not None)
        third = r.book(auto_advance=True, priority="lendable")
        r.check("the card is full, so the third is assigned later",
                third is not None and third["assigned_later"] is True,
                "" if third is None else third["claim"]["start"])

        # 3. releasing the first early pulls the third earlier
        status, out = call("POST", f"{r.base}/api/claims/{a['id']}/release", {})
        moved = [m for m in (out or {}).get("advanced", [])
                 if third and m["id"] == third["claim"]["id"]]
        r.check("an early release pulls the assigned slot earlier",
                status == 200 and bool(moved),
                "" if not moved else f"{moved[0]['old_start']} -> {moved[0]['new_start']}")

        # 4. a watcher is parked on the second booking, then an important booking takes it
        got: list = []
        watcher = threading.Thread(
            target=lambda: got.append(
                call("GET", f"{r.base}/api/claims/{second['claim']['id']}/events",
                     timeout=90)),
            daemon=True)
        watcher.start()
        time.sleep(1.0)                      # let the connection park
        began = time.monotonic()
        important = r.book(priority="important", vram_gib=free // 1024,
                           duration_minutes=5)
        r.check("an important booking succeeds against a lendable one",
                important is not None)
        watcher.join(30)
        waited = time.monotonic() - began
        r.check("the watcher was woken, not polled", bool(got) and got[0][0] == 200,
                f"{waited:.2f}s")
        if got and got[0][0] == 200:
            ev = got[0][1]
            r.check("the event says the card was taken and by whom",
                    ev.get("event") == "taken" and bool(ev.get("by")), json.dumps(ev))
            r.check("the notification arrived in under two seconds", waited < 2.0,
                    f"{waited:.2f}s")

        # 5. the revoked booking was truncated, not cancelled
        status, out = call("GET", f"{r.base}/api/claims")
        rows = {c["id"]: c for c in (out or {}).get("claims", [])}
        victim = rows.get(second["claim"]["id"], {})
        r.check("the revoked booking is truncated, not cancelled",
                victim.get("cancelled_at") is None
                and victim.get("end") < second["claim"]["end"],
                f"{second['claim']['end']} -> {victim.get('end')}")
    finally:
        r.cleanup()

    print(f"\n{'all steps passed' if r.failures == 0 else str(r.failures) + ' step(s) failed'}")
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

The `GET /api/claims` reply's key for the booking list must match what `web.py:_claims`
actually sends; check it (`claims`) and fix the script's `out["claims"]` if it differs.

- [ ] **Step 4: Run it**

Locally first, against a scratch server with a fake history so `_cards()` has a card size:

```bash
python -m pytest tests/test_skills_present.py -k acceptance -v
python -m resourcemonitor serve --bind 127.0.0.1 --port 8799 \
  --claims /tmp/acc-claims.sqlite --policy deploy/policy.example.toml &
python3 deploy/acceptance-slots.py --base http://127.0.0.1:8799 --user "$(id -un)" --gib 30
```

Expected: every line PASS. On a laptop with no history the card size check fails by design —
run it against a host where `watch` has polled, which is the point of doing it on the t14.

Then on the t14, which the user drives when they are at the machine:

```bash
rsync -a --exclude .git --exclude __pycache__ ./ t14:~/ResourceMonitor/
ssh t14 'cd ~/ResourceMonitor && python3 -m resourcemonitor watch --interval 60' &   # one poll is enough
ssh t14 'cd ~/ResourceMonitor && ~/ResourceMonitor/deploy/run-resourcemonitor.sh serve' &
ssh t14 'cd ~/ResourceMonitor && python3 deploy/acceptance-slots.py \
  --base http://192.168.188.88:8765 --user christian --gib 6'
```

Expected on t14: a 16376 MiB card, two 6 GiB bookings fitting, the third assigned later, and
the `taken` notification in well under a second over the LAN.

- [ ] **Step 5: Document and commit**

In `README.md`, under the booking section, add the two new policy keys and three lines on
the acceptance run. In `.claude/skills/deploy-resourcemonitor/SKILL.md`, add a step after
the dry run: *run `deploy/acceptance-slots.py` against the host before enabling the unit;
it needs one `watch` poll first or it will refuse, because without history the server
assumes 80 GB cards.*

```bash
git add deploy/acceptance-slots.py README.md .claude/skills/deploy-resourcemonitor/SKILL.md tests/test_skills_present.py
git commit -m "test(deploy): end-to-end acceptance run for assigned slots and revocation"
```

---

## After the last task

- `python -m pytest -q` — expect the pre-existing 447 plus roughly 110 new tests, and the
  one node skip where node is absent.
- Deploy order from the spec, unchanged: olive, then carrot (needs `host.env` with
  `RESOURCEMONITOR_BIND=10.188.1.1` first), then t14 as its own booking domain. The schema
  migrates on first write and the API is additive, so an old client keeps working.
- The `hrm` SDK is the next plan, in its own repo, against `deploy/acceptance-slots.py`.
