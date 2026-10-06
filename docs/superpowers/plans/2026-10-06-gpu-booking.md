# GPU Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let people book a share of a GPU (VRAM for a time window) in a web form on the dashboard, and alert on use of capacity someone else booked or beyond your own booking. Bookings replace the fixed GPU split.

**Architecture:**
- A new `claims.py` owns `claims.sqlite`. It is the only writer, and only the web process calls its write methods.
- `watch` reads the active bookings each poll, read-only and fail-open, and evaluates a new `check_bookings` rule in place of `check_allocation`.
- The web server gains `GET /api/claims`, `GET /api/users`, `POST /api/claims` and `POST /api/claims/<id>/cancel`, with drive-by protections.
- The page gains a Bookings section: a form, a calendar and recent changes. The Now cards and the Timeline show bookings.

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `pwd`, `http.server`, `json`, `threading`), vanilla JS with SVG. Node is used only to unit-test one pure JS function.

**Spec:** `docs/superpowers/specs/2026-10-06-gpu-booking-design.md` (approved 2026-10-06). Read it. It is the authority this plan argues from.

## Global Constraints

- **Stdlib only.** Never touch, signal or reprioritise any process. Booking violations are reported, never enforced.
- **Write isolation.** Only the web process writes, and only `~/.local/state/resourcemonitor/claims.sqlite`. `history.sqlite` stays `mode=ro` for the web process. `watch` opens `claims.sqlite` with `mode=ro`.
- **The web process never imports `resourcemonitor.probe`, `resourcemonitor.notify` or `resourcemonitor.cli`.** The existing subprocess test must keep passing.
- **A missing, corrupt or locked `claims.sqlite` never breaks monitoring.** It means no active bookings, so everything is free. Errors are printed as the exception class name only.
- **Claims rows are never deleted.** Cancelling sets `cancelled_at`/`cancelled_ip`.
- **Timestamps:** stored as UTC ISO-8601 strings (`datetime.isoformat()` of an aware UTC datetime). API input must carry an offset; naive datetimes are rejected with 400.
- **Booking is active at t** when `cancelled_at IS NULL AND start <= t < end`. Start is inclusive, end is exclusive. Back-to-back bookings (`end == other.start`) do not overlap.
- **Limits:**
  - VRAM is 1 GiB (1024 MiB) up to the card total.
  - The card total is the latest poll's `total_mib` for that GPU, with a fallback of **81559**.
  - Duration ≤ **14 days**.
  - `start >= now - 5 min`.
  - Note ≤ **120** characters, no control characters.
- **Over-use tolerance:** `used_mib > 1.10 × booked_mib` raises an alert. Exactly 110 % does not.
- **Validation error details** come from fixed templates. Only validated values (user names matching `^[a-z_][a-z0-9._-]{0,31}$`, numbers, times) are interpolated, never raw input.
- **Write protections:**
  - `Content-Type` must be `application/json` (else 415).
  - The body must be ≤ 4096 bytes (else 413).
  - If `Origin` is present it must equal `http://<Host header>` (else 403).
  - At most 30 writes per client IP per rolling hour (else 429).
  - No CORS headers ever.
- **Every response** carries `Content-Security-Policy: default-src 'self'`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- **Reserved bucket/user words** stay as before. Booking users must be real login accounts: a `pwd` entry with UID ≥ 1000, via an injectable directory.
- **Commits carry NO `Co-Authored-By` trailer or any AI-attribution line** (user instruction).

## Review Focus

1. **Offset-carrying times across DST and timezones.** For example `2026-10-25T02:30:00+02:00` must be stored as the right UTC instant, and naive `2026-10-25T02:30` must be rejected (400). Tested in Task 1.
2. **Back-to-back bookings.** A booking ending at 18:00 and another starting at 18:00 on the same full card must both succeed. Tested in Task 1.
3. **A booking expiring while a job runs.** At the first poll after `end`, the GPU is free and the alert stops; the monitor must not keep accusing. Tested in Task 2.
4. **No history yet** (fresh install). Booking still works, using the card-total fallback of 81559 MiB. Tested in Task 5.
5. **Cancelling someone else's booking, or cancelling twice.** Allowed (honour system), recorded with IP; a second cancel is a 200 no-op that keeps the first audit values. Tested in Task 1 and Task 5.

---

## File Structure

```
resourcemonitor/
  claims.py      CREATE  Booking, ClaimError, UserDirectory/PwdUsers, ClaimsStore (only writer), load_active (ro, fail-open)
  rules.py       MODIFY  check_bookings replaces check_allocation; check_unattributed takes bookings
  policy.py      MODIFY  [assignments] optional, ignored by rules
  cli.py         MODIFY  --claims flag; run_once loads active bookings, calls check_bookings
  queries.py     MODIFY  latest() takes bookings instead of assignments
  web.py         MODIFY  GET /api/claims, GET /api/users, POST create/cancel, protections, --claims flag
  web/index.html, web/app.js, web/app.css   MODIFY  Bookings section, form, calendar, cards, timeline bands
tests/
  claims_fixture.py  CREATE  FakeUsers directory + helper to build bookings
  test_claims.py test_rules_bookings.py  CREATE;  test_rules_stateless.py test_policy.py test_cli.py test_queries.py test_web.py test_web_static.py  MODIFY
  js/test_free_vram.mjs  CREATE  node test of the pure JS function
deploy/, README.md, CLAUDE.md, .claude/skills/deploy-resourcemonitor/SKILL.md, docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md  MODIFY (Task 7)
```

---

### Task 1: Claims store

**Files:**
- Create: `resourcemonitor/claims.py`, `tests/claims_fixture.py`
- Test: `tests/test_claims.py`

**Interfaces — Produces:**

```python
MIB_PER_GIB = 1024
DEFAULT_CARD_MIB = 81559
MAX_DAYS = 14
PAST_SLACK = timedelta(minutes=5)
NOTE_MAX = 120

@dataclass(frozen=True)
class Booking:
    id: int; user: str; gpu: int; vram_mib: int
    start: datetime; end: datetime          # aware UTC
    note: str | None
    created_at: datetime; created_ip: str
    cancelled_at: datetime | None = None; cancelled_ip: str | None = None
    def active_at(self, t: datetime) -> bool: ...   # cancelled_at is None and start <= t < end
    def to_json(self) -> dict: ...                   # ISO strings, vram_gib = round(vram_mib/1024, 1) plus vram_mib

class ClaimError(Exception):
    def __init__(self, status: int, detail: str): ...   # status 400 or 409 (or 404 for cancel)
    status: int; detail: str

class UserDirectory(Protocol):
    def known(self, name: str) -> bool: ...
    def all(self) -> list[str]: ...

class PwdUsers:                     # real accounts: pwd entries with uid >= 1000 and a valid name
    def __init__(self, min_uid: int = 1000): ...

def parse_time(s: str) -> datetime   # ISO with offset -> aware UTC; naive or garbage -> ClaimError(400, "times need a date, time and UTC offset")

class ClaimsStore:
    def __init__(self, path: Path | str, users: UserDirectory | None = None): ...   # creates schema (WAL, user_version=1); users default PwdUsers()
    def create(self, *, user: str, gpu: int, vram_mib: int, start: datetime, end: datetime,
               note: str | None, ip: str, now: datetime, card_mib: int = DEFAULT_CARD_MIB) -> Booking
    def cancel(self, claim_id: int, *, ip: str, now: datetime) -> Booking     # unknown id -> ClaimError(404, "no such booking")
    def get(self, claim_id: int) -> Booking | None
    def list_window(self, now: datetime, days_ahead: int = 14, days_back: int = 7) -> list[Booking]
        # all rows (incl. cancelled) with end >= now - days_back and start <= now + days_ahead, ordered by start, id
    def close(self) -> None

def load_active(path: Path | str, t: datetime) -> list[Booking]
    # read-only (as_uri + "?mode=ro"); returns bookings active at t; returns [] when the file is
    # missing; re-raises nothing: on sqlite3.Error/OSError prints "claims unavailable: <Class>" and returns []
```

**Schema:** exactly as in the spec, Section 1.

**Validation order and exact details:**
1. `user` does not match `^[a-z_][a-z0-9._-]{0,31}$` → 400 `invalid user name`.
2. `not users.known(user)` → 400 `unknown user '<user>'`.
3. `gpu` not in 0..7 → 400 `GPU must be 0–7`.
4. `vram_mib < 1024 or vram_mib > card_mib` → 400 `VRAM must be between 1 and <card_mib//1024> GiB`.
5. `end <= start` → 400 `end must be after start`.
6. `start < now - PAST_SLACK` → 400 `start lies in the past`.
7. `end - start > 14 days` → 400 `a booking may last at most 14 days`.
8. `note` is longer than 120 characters → 400 `note too long`. It contains a character with `ord < 32` → 400 `invalid note`. An empty or whitespace-only note is stored as NULL.
9. **Capacity**, inside `BEGIN IMMEDIATE`:
   - Fetch the active (uncancelled) bookings on `gpu` overlapping `[start, end)`.
   - Collect boundary instants: `start` plus every overlapping booking's start that falls inside the window.
   - At each instant `t`, sum the VRAM of bookings active at `t` plus `vram_mib`.
   - If the sum exceeds `card_mib`, raise 409 `GPU <gpu> has only <free GiB, 1 decimal> GiB unbooked between <t1> and <t2>`. Here `t1` is the first conflicting instant and `t2` is the earliest end among the bookings active at `t1`, both formatted `%a %d %b %H:%M UTC`, and `free = card_mib - booked_at_t1` in GiB.

Use one connection per `ClaimsStore`, created with `check_same_thread=False` and `timeout=5`. Writes are serialized by `BEGIN IMMEDIATE`, and an internal `threading.Lock` protects the connection.

- [ ] **Step 1: Write the fixture helper** (`tests/claims_fixture.py`)

```python
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
USERS = ("cwinkelmann", "dorian.zwanzig", "andre.kliem")


class FakeUsers:
    def __init__(self, names=USERS):
        self._names = tuple(names)

    def known(self, name):
        return name in self._names

    def all(self):
        return sorted(self._names)


def book(store, user="dorian.zwanzig", gpu=4, gib=40, start=T0, hours=4, now=T0, ip="10.0.0.1",
         note=None, card_mib=81559):
    return store.create(user=user, gpu=gpu, vram_mib=gib * 1024, start=start,
                        end=start + timedelta(hours=hours), note=note, ip=ip, now=now,
                        card_mib=card_mib)
```

- [ ] **Step 2: Write the failing tests** (`tests/test_claims.py`)

```python
import sqlite3
import threading
from datetime import timedelta, timezone

import pytest

from resourcemonitor.claims import (Booking, ClaimError, ClaimsStore, load_active, parse_time)
from tests.claims_fixture import FakeUsers, T0, book


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "c.sqlite", users=FakeUsers())
    yield s
    s.close()


def _err(fn):
    with pytest.raises(ClaimError) as e:
        fn()
    return e.value.status, e.value.detail


def test_create_returns_the_stored_booking(store):
    b = book(store, note="KEV eval")
    assert (b.user, b.gpu, b.vram_mib, b.note, b.created_ip) == ("dorian.zwanzig", 4, 40960, "KEV eval", "10.0.0.1")
    assert b.start == T0 and b.end == T0 + timedelta(hours=4) and b.cancelled_at is None
    assert store.get(b.id) == b


@pytest.mark.parametrize("kwargs, status, detail", [
    ({"user": "Bad Name"}, 400, "invalid user name"),
    ({"user": "dorain.zwanzig"}, 400, "unknown user 'dorain.zwanzig'"),
    ({"gpu": 8}, 400, "GPU must be 0–7"),
    ({"gib": 0}, 400, "VRAM must be between 1 and 79 GiB"),
    ({"gib": 80}, 400, "VRAM must be between 1 and 79 GiB"),
    ({"hours": 0}, 400, "end must be after start"),
    ({"start": T0 - timedelta(minutes=6)}, 400, "start lies in the past"),
    ({"hours": 14 * 24 + 1}, 400, "a booking may last at most 14 days"),
    ({"note": "x" * 121}, 400, "note too long"),
    ({"note": "evil\x07"}, 400, "invalid note"),
])
def test_validation_errors_have_exact_details(store, kwargs, status, detail):
    assert _err(lambda: book(store, **kwargs)) == (status, detail)


def test_start_within_five_minutes_in_the_past_is_accepted(store):
    assert book(store, start=T0 - timedelta(minutes=4)).start == T0 - timedelta(minutes=4)


def test_exactly_fourteen_days_is_allowed(store):
    assert book(store, hours=14 * 24).end == T0 + timedelta(days=14)


def test_blank_note_is_stored_as_null(store):
    assert book(store, note="   ").note is None


def test_overbooking_is_409_with_the_free_amount(store):
    book(store, user="cwinkelmann", gib=40)
    status, detail = _err(lambda: book(store, user="andre.kliem", gib=40))
    assert status == 409
    assert detail.startswith("GPU 4 has only 39.6 GiB unbooked between Tue 06 Oct 12:00 UTC and Tue 06 Oct 16:00 UTC")


def test_two_shares_that_fit_both_succeed(store):
    book(store, user="cwinkelmann", gib=40)
    assert book(store, user="andre.kliem", gib=39).vram_mib == 39 * 1024


def test_back_to_back_full_card_bookings_do_not_conflict(store):
    book(store, gib=79, hours=4)
    assert book(store, user="cwinkelmann", gib=79, start=T0 + timedelta(hours=4)).gpu == 4


def test_capacity_is_checked_at_the_worst_instant(store):
    # A: 30 GiB 12-14, B: 30 GiB 13-16. New 30 GiB 12-16 collides only during 13-14 (90 > 79.6).
    book(store, user="cwinkelmann", gib=30, hours=2)
    book(store, user="andre.kliem", gib=30, start=T0 + timedelta(hours=1), hours=3)
    status, detail = _err(lambda: book(store, gib=30, hours=4))
    assert status == 409 and "between Tue 06 Oct 13:00 UTC and Tue 06 Oct 14:00 UTC" in detail


def test_other_gpus_and_cancelled_bookings_do_not_count(store):
    other = book(store, user="cwinkelmann", gpu=5, gib=79)
    gone = book(store, user="andre.kliem", gib=79)
    store.cancel(gone.id, ip="10.0.0.2", now=T0)
    assert book(store, gib=79).gpu == 4 and other.gpu == 5


def test_cancel_records_audit_and_is_idempotent(store):
    b = book(store)
    c1 = store.cancel(b.id, ip="10.0.0.9", now=T0 + timedelta(minutes=1))
    c2 = store.cancel(b.id, ip="10.0.0.7", now=T0 + timedelta(minutes=2))
    assert c1.cancelled_ip == "10.0.0.9" and c2 == c1          # second cancel changes nothing
    assert _err(lambda: store.cancel(9999, ip="x", now=T0)) == (404, "no such booking")


def test_rows_are_never_deleted(store, tmp_path):
    b = book(store)
    store.cancel(b.id, ip="10.0.0.9", now=T0)
    n = sqlite3.connect(tmp_path / "c.sqlite").execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert n == 1


def test_active_at_is_start_inclusive_end_exclusive(store):
    b = book(store)
    assert b.active_at(T0) and not b.active_at(T0 + timedelta(hours=4))
    assert not b.active_at(T0 - timedelta(seconds=1))


def test_list_window_includes_recent_cancelled_and_upcoming(store):
    past = book(store, start=T0, hours=1)
    upcoming = book(store, user="cwinkelmann", gpu=5, start=T0 + timedelta(days=3))
    store.cancel(past.id, ip="10.0.0.3", now=T0)
    ids = [b.id for b in store.list_window(T0 + timedelta(days=1), days_ahead=14, days_back=7)]
    assert ids == [past.id, upcoming.id]


def test_parallel_bookings_for_the_last_share_exactly_one_wins(tmp_path):
    path = tmp_path / "c.sqlite"
    ClaimsStore(path, users=FakeUsers()).close()
    results = []

    def attempt(user):
        s = ClaimsStore(path, users=FakeUsers())
        try:
            book(s, user=user, gib=60)
            results.append("ok")
        except ClaimError as e:
            results.append(e.status)
        finally:
            s.close()

    ts = [threading.Thread(target=attempt, args=(u,)) for u in ("cwinkelmann", "andre.kliem")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results, key=str) == [409, "ok"]


@pytest.mark.parametrize("text, expected", [
    ("2026-10-25T02:30:00+02:00", "2026-10-25T00:30:00+00:00"),
    ("2026-10-25T02:30:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-06T12:00:00Z", "2026-10-06T12:00:00+00:00"),
])
def test_parse_time_normalises_offsets_to_utc(text, expected):
    assert parse_time(text).isoformat() == expected


@pytest.mark.parametrize("text", ["2026-10-25T02:30", "tomorrow", "", "2026-13-01T00:00+00:00"])
def test_parse_time_rejects_naive_or_garbage(text):
    assert _err(lambda: parse_time(text)) == (400, "times need a date, time and UTC offset")


def test_load_active_is_read_only_and_fail_open(store, tmp_path, capsys):
    b = book(store)
    assert [x.id for x in load_active(tmp_path / "c.sqlite", T0 + timedelta(hours=1))] == [b.id]
    assert load_active(tmp_path / "c.sqlite", T0 + timedelta(hours=5)) == []
    assert load_active(tmp_path / "missing.sqlite", T0) == []
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database at all" * 100)
    assert load_active(bad, T0) == []
    assert "claims unavailable: DatabaseError" in capsys.readouterr().out
```

Note on the card total in tests: `81559 // 1024 == 79`, so the VRAM detail reads "between 1 and 79 GiB". The 409 free amount uses one decimal: `(81559 - 40960)/1024 = 39.6`.

- [ ] **Step 3: Run them and watch them fail.** `python3 -m pytest tests/test_claims.py -v` gives `ModuleNotFoundError: No module named 'resourcemonitor.claims'`.
- [ ] **Step 4: Implement** `resourcemonitor/claims.py` per the Interfaces and the validation order above.
  - `parse_time`: `datetime.fromisoformat(s.replace("Z", "+00:00"))`, then reject when `tzinfo is None`, then `.astimezone(timezone.utc)`. Map any `ValueError` to the 400.
  - `PwdUsers.known`: `pwd.getpwnam(name).pw_uid >= min_uid`, with `KeyError → False`.
  - `PwdUsers.all`: `pwd.getpwall()`, filtered by UID and name pattern, sorted. Exclude `nobody` (UID 65534).
- [ ] **Step 5: Run** `python3 -m pytest -q`. Expected: all pass (previously 183 passed, 1 skipped).
- [ ] **Step 6: Commit**

```bash
git add resourcemonitor/claims.py tests/claims_fixture.py tests/test_claims.py
git commit -m "feat(claims): booking store with validation, capacity check and audit trail"
```

---

### Task 2: Booking rules replace the fixed split

**Files:**
- Modify: `resourcemonitor/rules.py`, `resourcemonitor/policy.py`
- Test: create `tests/test_rules_bookings.py`; modify `tests/test_rules_stateless.py` and `tests/test_policy.py`

**Interfaces:**
- Consumes: `Booking` (Task 1).
- Produces:
  - `check_bookings(snap: Snapshot, pol: Policy, bookings: list[Booking]) -> list[Alert]`. Only bookings with `active_at(snap.taken_at)` count; the function filters.
  - `check_unattributed(snap, pol, bookings: list[Booking])`. **Deviation from the spec's "unattributed rule unchanged":** that rule only fired on *assigned* GPUs, so with assignments gone it would never fire. It now alerts only on GPUs with an active booking (consistent with "unbooked is free"), and its text says `(booked by <users>)`, not `(assigned to …)`.
  - **`check_allocation` is removed.**
  - `Policy.assignments` stays as a field (empty dict when the section is absent). `[assignments]` becomes optional in `load_policy`; when present, it is still validated.
  - `Policy.owner_of_gpu` is removed (nothing uses it after this task). Search the codebase; `queries.py` and `web.py` usages are replaced in Task 4. Until then, keep `owner_of_gpu` if removing it breaks Task 4 code, and note that in the report.

**Rule semantics** (from the spec, Section 1), per GPU `g` with active bookings `B_g`:
- If `B_g` is empty: no booking alert.
- Let `total = g.total_mib`, `booked = Σ b.vram_mib for b in B_g`, and `booked_by_user = {b.user: Σ vram}` (a user may hold several bookings on one card).
- Let `use_by_user` be the sum of `used_mib` for processes on `g` with a non-None user.
- For each user with `booked_by_user[user]`: if `use > 1.10 × booked_by_user[user]`, raise an `Alert(kind="over_booking", key=f"booking:over:{user}:{g}", gpu_index=g, user=user, text=f"{user} uses {_mib(use)} on GPU {g} but booked {_mib(booked_by_user[user])}.")`.
- Non-booking users share `remainder = max(total - booked, 0)`. If `Σ use of non-booking users > remainder`, then for **each** non-booking user on `g` raise `Alert(kind="booked_gpu", key=f"booking:other:{user}:{g}", gpu_index=g, user=user, text=f"{user} is using {_mib(use)} on GPU {g}; {_mib(booked)} is booked by {', '.join(sorted(booked_by_user))} until {earliest_end:%a %H:%M} UTC, {_mib(remainder)} unbooked.")`.

- [ ] **Step 1: Write the failing tests** (`tests/test_rules_bookings.py`)

```python
from datetime import datetime, timedelta, timezone

from resourcemonitor.claims import Booking
from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import check_bookings, check_unattributed

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
POL = Policy(assignments={}, idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
             capacity_free_mib=40960, cooldown_s=3600, channel="#c")
GIB = 1024


def _b(user, gpu=4, gib=40, start=T0 - timedelta(hours=1), hours=6, cancelled=False, id=1):
    return Booking(id=id, user=user, gpu=gpu, vram_mib=gib * GIB, start=start,
                   end=start + timedelta(hours=hours), note=None, created_at=start,
                   created_ip="10.0.0.1",
                   cancelled_at=start if cancelled else None,
                   cancelled_ip="10.0.0.2" if cancelled else None)


def _snap(procs, t=T0, gpu=4):
    return Snapshot(t, (GpuState(gpu, 81559, sum(p.used_mib for p in procs), 90),), tuple(procs))


def _p(user, mib, pid=1, gpu=4):
    return GpuProcess(pid, gpu, mib, user)


def test_unbooked_gpu_is_free_for_anyone():
    assert check_bookings(_snap([_p("andre.kliem", 70 * GIB)]), POL, []) == []


def test_within_own_share_is_fine_and_110_percent_is_still_fine():
    b = [_b("dorian.zwanzig", gib=40)]
    assert check_bookings(_snap([_p("dorian.zwanzig", 44 * GIB)]), POL, b) == []   # exactly 110 %


def test_over_own_share_beyond_tolerance_alerts():
    b = [_b("dorian.zwanzig", gib=40)]
    (a,) = check_bookings(_snap([_p("dorian.zwanzig", 44 * GIB + 1)]), POL, b)
    assert (a.kind, a.key, a.user, a.gpu_index) == ("over_booking", "booking:over:dorian.zwanzig:4", "dorian.zwanzig", 4)
    assert "booked 40.0 GiB" in a.text


def test_non_booker_within_the_unbooked_remainder_is_fine():
    b = [_b("dorian.zwanzig", gib=40)]
    procs = [_p("dorian.zwanzig", 30 * GIB), _p("andre.kliem", 39 * GIB, pid=2)]
    assert check_bookings(_snap(procs), POL, b) == []


def test_non_bookers_together_beyond_the_remainder_all_alert():
    b = [_b("dorian.zwanzig", gib=60)]
    procs = [_p("andre.kliem", 12 * GIB, pid=2), _p("cwinkelmann", 10 * GIB, pid=3)]
    alerts = check_bookings(_snap(procs), POL, b)
    assert {(a.kind, a.user) for a in alerts} == {("booked_gpu", "andre.kliem"), ("booked_gpu", "cwinkelmann")}
    assert all("booked by dorian.zwanzig" in a.text and "unbooked" in a.text for a in alerts)


def test_a_user_with_two_processes_is_one_incident_with_summed_vram():
    b = [_b("dorian.zwanzig", gib=40)]
    procs = [_p("dorian.zwanzig", 30 * GIB), _p("dorian.zwanzig", 20 * GIB, pid=2)]
    (a,) = check_bookings(_snap(procs), POL, b)
    assert a.kind == "over_booking" and "50.0 GiB" in a.text


def test_two_bookings_by_one_user_add_up():
    b = [_b("dorian.zwanzig", gib=20, id=1), _b("dorian.zwanzig", gib=20, id=2)]
    assert check_bookings(_snap([_p("dorian.zwanzig", 40 * GIB)]), POL, b) == []


def test_expired_and_cancelled_bookings_are_ignored():
    expired = _b("dorian.zwanzig", gib=79, start=T0 - timedelta(hours=5), hours=5)   # ends exactly at T0
    cancelled = _b("cwinkelmann", gib=79, cancelled=True, id=2)
    assert check_bookings(_snap([_p("andre.kliem", 70 * GIB)]), POL, [expired, cancelled]) == []


def test_alert_stops_at_the_first_poll_after_the_booking_ends():
    b = [_b("dorian.zwanzig", gib=79, start=T0 - timedelta(hours=1), hours=2)]
    busy = [_p("andre.kliem", 30 * GIB)]
    assert check_bookings(_snap(busy, t=T0), POL, b)                                 # booked: alert
    assert check_bookings(_snap(busy, t=T0 + timedelta(hours=1)), POL, b) == []      # ended: free


def test_unattributed_processes_never_trigger_booking_alerts():
    b = [_b("dorian.zwanzig", gib=79)]
    assert check_bookings(_snap([GpuProcess(9, 4, 30 * GIB, None)]), POL, b) == []


def test_unattributed_rule_fires_on_booked_gpus_only():
    procs = [GpuProcess(9, 4, 2 * GIB, None)]
    assert check_unattributed(_snap(procs), POL, []) == []
    (a,) = check_unattributed(_snap(procs), POL, [_b("dorian.zwanzig")])
    assert a.kind == "unattributed" and "booked by dorian.zwanzig" in a.text
```

  In `tests/test_rules_stateless.py`, delete the `check_allocation` tests and keep the capacity tests. In `tests/test_policy.py`, add:

```python
def test_assignments_section_is_optional(tmp_path):
    body = GOOD.split("[rules]", 1)[1]
    pol = load_policy(_write(tmp_path, "[rules]" + body))
    assert pol.assignments == {}
```

  Also change `test_missing_section_names_the_section` so it removes `[rules]` rather than `[assignments]`, if needed. It must still pass with a clear "rules" message.
- [ ] **Step 2: Run them and watch them fail** (`ImportError: cannot import name 'check_bookings'`).
- [ ] **Step 3: Implement** per the Interfaces and Rule semantics. Remove `check_allocation`. Update the `Alert.kind` comment to `"over_booking" | "booked_gpu" | "idle" | "capacity" | "unattributed" | "report"`.
- [ ] **Step 4: Run** `python3 -m pytest -q`. `cli.py` still imports `check_allocation`; fix that import in this task by temporarily calling `check_bookings(snap, pol, [])`, so the suite stays green. Task 3 wires the real bookings.
- [ ] **Step 5: Commit** `git commit -m "feat(rules): booking rules replace the fixed GPU split"`

---

### Task 3: `watch` evaluates active bookings

**Files:** Modify `resourcemonitor/cli.py`, `resourcemonitor/claims.py` (add `DEFAULT_CLAIMS = Path.home() / ".local/state/resourcemonitor/claims.sqlite"`). Test: `tests/test_cli.py`.

**Interfaces:**
- Consumes: `load_active(path, t)` (Task 1); `check_bookings`, `check_unattributed(…, bookings)` (Task 2).
- Produces:
  - The CLI flag `--claims PATH` (default `~/.local/state/resourcemonitor/claims.sqlite`), defined as `DEFAULT_CLAIMS` in `claims.py` and imported by both cli and web.
  - `run_once(pol, state, notifier, tracker, ledger, host, energy_path, history=None, claims_path=None)`. When `claims_path` is set, `bookings = load_active(claims_path, snap.taken_at)`; otherwise `[]`.
  - `main` passes `args.claims` for `once` and `watch`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_cli.py`, reusing that file's `_history_setup` and fake probe or notifier pattern. Read the file first.

```python
def test_run_once_alerts_on_someone_elses_booking(tmp_path, monkeypatch):
    # fake probe: andre.kliem holds 70 GiB on GPU 4; a booking by dorian.zwanzig for 60 GiB is active
    ...
    from resourcemonitor.claims import ClaimsStore
    from tests.claims_fixture import FakeUsers
    s = ClaimsStore(tmp_path / "c.sqlite", users=FakeUsers())
    s.create(user="dorian.zwanzig", gpu=4, vram_mib=60 * 1024, start=t0, end=t0 + timedelta(hours=4),
             note=None, ip="10.0.0.1", now=t0)
    s.close()
    run_once(pol, state, notifier, tracker, ledger, "carrot", tmp_path / "e.json",
             claims_path=tmp_path / "c.sqlite")
    assert any(a.kind == "booked_gpu" for a in notifier_alerts)


def test_a_broken_claims_db_never_stops_the_other_rules(tmp_path, monkeypatch, capsys):
    (tmp_path / "c.sqlite").write_bytes(b"garbage" * 500)
    # fake probe: a full box so the capacity rule fires
    ...
    run_once(..., claims_path=tmp_path / "c.sqlite")
    assert any(a.kind == "capacity" for a in notifier_alerts)
    assert "claims unavailable: DatabaseError" in capsys.readouterr().out


def test_claims_flag_parses():
    assert str(build_parser().parse_args(["watch", "--claims", "/x/c.sqlite"]).claims) == "/x/c.sqlite"
```

  The `...` lines mean: write out the full setup from the existing tests in this file, in full and not by reference. `notifier_alerts` is whatever the existing fake notifier records (its `.sent` attribute holds a list of alert lists; flatten it).
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** In `run_once`, `alerts = check_bookings(snap, pol, bookings) + check_capacity(snap, pol) + check_unattributed(snap, pol, bookings) + tracker.observe(snap, pol)`.
- [ ] **Step 4: Run** `python3 -m pytest -q`.
- [ ] **Step 5: Commit** `git commit -m "feat(cli): watch reads active bookings (read-only, fail-open)"`

---

### Task 4: Read side — bookings in `/api/now`, `GET /api/claims`, `GET /api/users`

**Files:** Modify `resourcemonitor/queries.py`, `resourcemonitor/web.py`, `resourcemonitor/claims.py` (add `list_window_ro(path, now, days_ahead, days_back) -> list[Booking]`, read-only like `load_active`, `[]` when the file is missing). Test: `tests/test_queries.py`, `tests/test_web.py`, `tests/test_claims.py` (one test for `list_window_ro`).

**Interfaces:**
- Consumes: `Booking.to_json()`, `ClaimsStore.list_window`, `load_active`, `UserDirectory`, `PwdUsers` and `DEFAULT_CLAIMS` (Task 1).
- Produces:
  - `queries.latest(conn, bookings: list[Booking], now, stale_after_s)`. The `assignments` parameter is removed. Each GPU entry loses `assigned_to` and gains:
    - `"bookings"`: the active ones on that GPU, as `to_json()`, sorted by start;
    - `"booked_mib"`: an int;
    - `"free_mib"`: `max(total_mib - booked_mib, 0)`.
  - `make_server(bind, port, history_path, policy_path, stale_after_s=180, clock=…, claims_path=DEFAULT_CLAIMS, users: UserDirectory | None = None)`. `users` defaults to `PwdUsers()`.
  - The `serve` CLI gains `--claims PATH`.
  - `GET /api/claims?days=N` (N in 1..14, default 14) returns `{"now": iso, "claims": [b.to_json() for b in list_window(now, days_ahead=N, days_back=7)]}`. The handler opens the claims DB **read-only** for GETs: add `claims.list_window_ro(path, now, days_ahead, days_back)` (returns `[]` if the file is missing). The write store is only used by POST (Task 5).
  - `GET /api/users` returns `{"users": users.all()}`.
  - `/api/now` uses `load_active(claims_path, now)` for its bookings.
- Unknown query params are still 400. `days=0` and `days=15` are 400.

- [ ] **Step 1: Failing tests**
  - In `test_queries.py`, change the existing `latest` tests to pass `bookings=[]` and assert that `assigned_to` is gone. Add a test where one active booking on GPU 6 gives `bookings[0]["user"] == "cwinkelmann"`, `booked_mib == 40960` and `free_mib == total - 40960`.
  - In `test_web.py`, start the server with a temp `claims_path` and `users=FakeUsers()`, seed one booking through `ClaimsStore`, and assert:
    - `/api/claims` 200 lists it;
    - `/api/claims?days=15` is 400;
    - `/api/users` returns the fake users;
    - `/api/now` includes the booking on its GPU;
    - a missing claims file gives `/api/claims` → `{"claims": []}` and `/api/now` still 200;
    - the headers are present on all of these.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** Remove `_assignments` from web.py. Remove `Policy.owner_of_gpu` if Task 2 kept it.
- [ ] **Step 4: Run** `python3 -m pytest -q`. The page still references `assigned_to`; that's fine until Task 6, and the static tests don't check it.
- [ ] **Step 5: Commit** `git commit -m "feat(web): bookings in /api/now, GET /api/claims and /api/users"`

---

### Task 5: Write path — `POST /api/claims` and `/cancel`

**Files:** Modify `resourcemonitor/web.py`. Test: create `tests/test_web_claims.py`.

**Interfaces:**
- Consumes: `ClaimsStore`, `ClaimError`, `parse_time`, `DEFAULT_CARD_MIB` (Task 1); `queries.latest`/`open_ro` for the card total.
- Produces:
  - `do_POST` handles exactly `/api/claims` and `/api/claims/<id>/cancel` (`id` is digits only, matched by regex `^/api/claims/(\d{1,9})/cancel$`). Any other POST path is 404. GET-only paths reject POST with 405 (`/api/now` → 405, as today).
  - One `ClaimsStore` per server, opened lazily on the first write and protected by its internal lock.
  - A module-level `_RateLimiter(limit=30, window_s=3600)` keyed by `client_address[0]`. Its `allow(ip, now) -> bool` keeps a deque of timestamps per IP.

**Request handling order for POST:**
1. Route match, else 404.
2. Rate limit, else 429 `{"error": "too many requests"}`.
3. `Origin` present and `!= "http://" + Host header` → 403 `{"error": "forbidden"}`.
4. `Content-Type` (before `;`) is not `application/json` → 415 `{"error": "unsupported media type"}`.
5. `Content-Length` missing or `> 4096` → 413 `{"error": "too large"}`. Read exactly that many bytes.
6. Parse JSON. Not a dict → 400 `{"error": "invalid", "detail": "body must be a JSON object"}`.
7. Create:
   - Fields `user` (str), `gpu` (int), `vram_gib` (number > 0), `start` and `end` (str, through `parse_time`), and optional `note` (str or null).
   - Wrong types or missing fields → 400 with detail `missing or invalid field '<name>'`, where `<name>` comes from the fixed field list.
   - Unknown extra fields → 400 `unknown field`.
   - `vram_mib = round(vram_gib * 1024)`.
   - `card_mib` is the latest poll's `total_mib` for that GPU via `open_ro(history_path)`. Use `DEFAULT_CARD_MIB` on `NoHistory` or any sqlite error.
   - `ip = client_address[0]`; `now = server.clock()`.
   - 201 `{"claim": b.to_json()}`.
8. Cancel: the body may be `{}`. 200 `{"claim": b.to_json()}`.
9. `ClaimError(e)` → `e.status` with `{"error": {400: "invalid", 404: "not found", 409: "conflict"}[e.status], "detail": e.detail}`.

Writes are **not** gated by `_QUERY_SLOTS`. Every response goes through `_send`, so the headers are present.

- [ ] **Step 1: Failing tests** (`tests/test_web_claims.py`). Start the server with `clock=lambda: T0`, `users=FakeUsers()`, a temp history built by `tests.history_fixture.build_history` (module-scoped), and a temp `claims_path`. Use a helper `_post(url, obj, headers=None, raw=None)` built on `urllib.request.Request(method="POST")` that defaults `Content-Type: application/json`.

```python
def test_create_then_cancel_round_trip(live):
    s, _, body = _post(live + "/api/claims", {"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 40,
                       "start": "2026-10-06T14:00:00+02:00", "end": "2026-10-06T18:00:00+02:00",
                       "note": "KEV eval"})
    c = json.loads(body)["claim"]
    assert s == 201 and c["start"] == "2026-10-06T12:00:00+00:00" and c["vram_mib"] == 40960
    s, _, body = _post(live + f"/api/claims/{c['id']}/cancel", {})
    assert s == 200 and json.loads(body)["claim"]["cancelled_ip"] == "127.0.0.1"
    s, _, body = _post(live + f"/api/claims/{c['id']}/cancel", {})
    assert s == 200                                              # idempotent


@pytest.mark.parametrize("payload, status, detail", [
    ({"user": "dorain", "gpu": 4, "vram_gib": 1, "start": S, "end": E}, 400, "unknown user 'dorain'"),
    ({"user": "dorian.zwanzig", "gpu": "4", "vram_gib": 1, "start": S, "end": E}, 400, "missing or invalid field 'gpu'"),
    ({"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 1, "start": "2026-10-06T14:00", "end": E}, 400, "times need a date, time and UTC offset"),
    ({"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 1, "start": S, "end": E, "x": 1}, 400, "unknown field"),
])
def test_bad_bookings_get_a_specific_400(live, payload, status, detail):
    s, _, body = _post(live + "/api/claims", payload)
    assert (s, json.loads(body)) == (status, {"error": "invalid", "detail": detail})


def test_overbooking_is_409(live): ...            # book 60 GiB twice on the same GPU and window -> second 409 with "unbooked between"
def test_wrong_content_type_is_415(live): ...     # Content-Type: text/plain -> 415
def test_foreign_origin_is_403(live): ...         # Origin: http://evil.example -> 403; Origin equal to http://127.0.0.1:<port> -> 201
def test_oversize_body_is_413(live): ...          # raw body of 5000 bytes -> 413
def test_rate_limit_is_429_after_30_writes(live, monkeypatch): ...   # monkeypatch the limiter to limit=2 for speed; third write -> 429
def test_unknown_cancel_id_is_404_and_bad_path_is_404(live): ...    # /api/claims/999/cancel -> 404; /api/claims/abc/cancel -> 404
def test_post_to_read_routes_is_405(live): ...    # POST /api/now -> 405
def test_booking_works_without_history_using_card_fallback(tmp_path): ...  # server with a non-existent history: 79 GiB accepted, 80 GiB -> 400 "VRAM must be between 1 and 79 GiB"
def test_every_write_response_carries_the_security_headers(live): ...     # CSP, nosniff, no-store on 201/400/403/409/413/415/429; no Access-Control-Allow-Origin
def test_history_stays_unwritable_and_serve_imports_nothing_forbidden(): ...  # re-run the existing subprocess import check; open_ro write still raises
```

  The `...` stubs above are part of this task: write each test out in full, following the inline comment. `S = "2026-10-06T14:00:00+02:00"` and `E = "2026-10-06T18:00:00+02:00"`.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement** per the handling order.
- [ ] **Step 4: Run** `python3 -m pytest -q`.
- [ ] **Step 5: Commit** `git commit -m "feat(web): create and cancel bookings (honour system, drive-by protections)"`

---

### Task 6: The page — Bookings section, cards, timeline bands

**Files:** Modify `resourcemonitor/web/index.html`, `app.js` and `app.css`. Test: `tests/test_web_static.py`. Create `tests/js/test_free_vram.mjs`.

**Interfaces:**
- Consumes: `/api/now` (GPU entries with `bookings`, `booked_mib` and `free_mib`), `GET /api/claims`, `GET /api/users`, `POST /api/claims` and `POST /api/claims/<id>/cancel` (Tasks 4–5). Read web.py and queries.py for the exact shapes.
- Produces: a pure function in `app.js`, `freeVramMiB(claims, gpu, startMs, endMs, cardMiB) -> number`. It returns the minimum free MiB over `[startMs, endMs)`, counting only uncancelled claims on that GPU (start inclusive, end exclusive). The form preview and the GPU options use it. Export it for Node with `if (typeof module !== "undefined") module.exports = { freeVramMiB };`, which is CSP-safe and harmless in the browser.

**Design** (spec, Section 3):
- **Bookings section** (`id="bookings"`), placed directly after Now.
  - **Form** (`id="book-form"`) with these fields:
    - user `<select>`, filled from `/api/users`; the last choice is kept in `localStorage` key `rm.user`, with every access wrapped in try/catch;
    - GPU `<select>` 0–7, with labels like "GPU 4 — 39.6 GiB free", recomputed when the window changes;
    - VRAM number in GiB, `step=1`, `max` = free;
    - from `datetime-local`, defaulting to now (rounded down to the minute);
    - until: buttons **+4 h**, **+1 day**, **Fri 18:00** (the next Friday 18:00 local, or the one after if that is already past) and a **custom** `datetime-local`;
    - note (maxlength 120);
    - a live preview line;
    - submit;
    - an inline error area (`id="book-error"`) that shows the server's `detail` verbatim through `textContent`.
  - Times are sent as ISO with the browser's offset: build `YYYY-MM-DDTHH:MM:00±HH:MM` from `getTimezoneOffset()`.
  - **Calendar** (`id="booking-calendar"`): one SVG with rows for GPUs 0–7, from now to +14 days, with day ticks. Each uncancelled claim is a bar whose height is proportional to `vram_mib / card total`. Bars are stacked within the row by greedy lanes ordered by start, in the user's colour, labelled `user · NN GiB` when wide enough. Clicking a bar shows its details panel (`id="booking-detail"`) with a Cancel button. The two-step confirmation is "Cancel booking?" → "Yes, cancel" / "Keep", and it POSTs the cancel.
  - **Recent changes** (`id="booking-changes"`): the last 20 events from `/api/claims`. Each claim yields a "booked" event at `created_at` and, if cancelled, a "cancelled" event at `cancelled_at`. Sort newest first. Format: `HH:MM · booked GPU 4, 40 GiB, for dorian.zwanzig until Fri 18:00 · from 10.188.3.7`.
  - Refresh `/api/claims` every 60 s and immediately after a successful create or cancel. Never add query params beyond `days`.
- **Now cards:**
  - Replace "assigned to X" with the active bookings (`user NN GiB until Fri 18:00`, up to 2 lines, then "+n more") and "NN GiB free".
  - The VRAM bar shows outlined segments for booked shares with actual usage filled on top.
  - A card is red only when an alert with `kind` `booked_gpu` or `over_booking` exists for that GPU. Remove the "outside allocation" tag and the `isOutside` logic.
- **Timeline:**
  - Fetch `/api/claims?days=1` and draw past and current bookings as faint bands (fill-opacity about 0.12, in the user's colour) behind job bars in each GPU row, clipped to the axis.
  - A job bar gets the red outline when its user had no booking on that GPU while someone else's booking overlapped the job (spec, Section 3). Replace the old `isOutside` use.
- **Safety:**
  - `textContent`/`setAttribute` only, no `innerHTML`, no inline script or style, no URL literals (keep the `SVG_NS` constant trick).
  - Form inputs are validated by the server; the client checks are only for convenience.
  - POST bodies are JSON with `Content-Type: application/json`, sent with `fetch(…, {method: "POST", headers: {"Content-Type": "application/json"}, body})`.

- [ ] **Step 1: Failing tests**
  - In `tests/test_web_static.py`, add `bookings`, `book-form`, `book-error`, `booking-calendar`, `booking-detail` and `booking-changes` to the required ids.
  - Assert that app.js contains no `assigned_to` and no `confirm(`.
  - Create `tests/js/test_free_vram.mjs`:

```js
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { freeVramMiB } = createRequire(import.meta.url)("../../resourcemonitor/web/app.js");
const H = 3600e3, card = 81559;
const c = (gpu, vram_mib, s, e, cancelled_at = null) => ({ gpu, vram_mib, start: new Date(s * H).toISOString(), end: new Date(e * H).toISOString(), cancelled_at });
assert.equal(freeVramMiB([], 4, 0, 4 * H, card), card);
assert.equal(freeVramMiB([c(4, 40960, 0, 2)], 4, 0, 4 * H, card), card - 40960);
assert.equal(freeVramMiB([c(4, 30720, 0, 2), c(4, 30720, 1, 3)], 4, 0, 4 * H, card), card - 61440);  // worst instant 1–2 h
assert.equal(freeVramMiB([c(4, 40960, 0, 2)], 4, 2 * H, 4 * H, card), card);                          // back-to-back
assert.equal(freeVramMiB([c(4, 40960, 0, 2, "x")], 4, 0, 4 * H, card), card);                         // cancelled
assert.equal(freeVramMiB([c(5, 40960, 0, 2)], 4, 0, 4 * H, card), card);                              // other GPU
console.log("ok");
```

  Add a pytest wrapper in `tests/test_web_static.py` that runs `node tests/js/test_free_vram.mjs` when `shutil.which("node")` is available (skip otherwise) and asserts that the output contains `ok`. `app.js` must load in Node without touching `document` at import time: guard the start-up code with `if (typeof document !== "undefined")`.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement** the design.
- [ ] **Step 4: Run** `python3 -m pytest -q`.
- [ ] **Step 5: Local visual check.**
  - Build the preview history: `build_history` over 72 h, ending now.
  - Seed 3 bookings into a temp claims DB through `ClaimsStore` with `FakeUsers`: dorian GPU 6 40 GiB from now to +2 d; andre GPU 0 79 GiB +1 d → +3 d; cwinkelmann GPU 4 30 GiB from now to +6 h.
  - Serve with `--claims` pointing at it on a free port, with `--stale-after 100000`.
  - Note: `serve` uses `PwdUsers` for `/api/users`. On the Mac this lists Mac accounts, which is fine for the visual check, and booking through the form works for a Mac account.
  - With Playwright: book through the form, see the booking in the calendar and the recent changes, cancel it through the two-step flow, and check there are no console errors.
  - Save a full-page screenshot to `/private/tmp/claude-501/-Users-christian-work-ResourceMonitor/febe6c9a-8da2-423a-ab2a-2f4a2b41a6aa/scratchpad/booking-preview.png`. Stop the server.
- [ ] **Step 6: Commit** `git commit -m "feat(web): booking form, calendar and recent changes; bookings in cards and timeline"`

---

### Task 7: Docs, deploy and end-to-end on carrot

**Files:** Modify `README.md`, `CLAUDE.md` (project section), `.claude/skills/deploy-resourcemonitor/SKILL.md`, `docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md` (the "Deferred" bullet only) and `docs/superpowers/plans/2026-10-06-history-dashboard.md` (the fixed-route Global Constraints line only). Test: `tests/test_skills_present.py`.

- [ ] **Step 1: Failing test.** Append to `tests/test_skills_present.py`:

```python
def test_docs_describe_booking_honestly():
    readme = (ROOT / "README.md").read_text()
    assert "Booking a GPU" in readme and "honour system" in readme and "never enforced" in readme
    claude = (ROOT / "CLAUDE.md").read_text()
    assert "claims.sqlite" in claude
```

- [ ] **Step 2: Docs.**
  - README "Booking a GPU": how to book and cancel; that anyone on the LAN can book or cancel in any name (honour system) and every change is shown with time and IP; that bookings are reported but **never enforced**; the alert rules (110 % tolerance, unbooked remainder is free); the 14-day maximum.
  - CLAUDE.md, one line: "The web process writes only `claims.sqlite` (bookings); `history.sqlite` stays read-only to it."
  - Deploy skill: the new file `~/.local/state/resourcemonitor/claims.sqlite`, and that the soak and the web unit must both be restarted after a code update.
  - v1 plan "Deferred: Reserving/queueing GPUs": append "— superseded 2026-10-06: see docs/superpowers/specs/2026-10-06-gpu-booking-design.md".
  - Dashboard plan route constraint: add the four booking routes.
- [ ] **Step 3: Run** `python3 -m pytest -q`, then commit: `git commit -m "docs: GPU booking (honour system, never enforced)"`
- [ ] **Step 4: Roll out to carrot.** SSH authorized; use exactly `ssh -o BatchMode=yes cwinkelmann@10.188.1.1 '...'`.
  - Never use `--post`. Never touch `~/.config/resourcemonitor/env`. Do not install `resourcemonitor.service`.
  1. `rsync -a --exclude .git --exclude .superpowers --exclude __pycache__ --exclude .idea --exclude .pytest_cache --exclude .claude/settings.local.json --exclude .playwright-mcp ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/`
  2. `systemctl --user restart resourcemonitor-soak resourcemonitor-web; sleep 75; systemctl --user is-active resourcemonitor-soak resourcemonitor-web`
  3. **End to end from this Mac** with python urllib:
     - `GET http://10.188.1.1:8765/api/users` must list `cwinkelmann`, `dorian.zwanzig` and `andre.kliem`.
     - `POST /api/claims` with `{"user": "cwinkelmann", "gpu": 5, "vram_gib": 1, "start": <now+2 min with offset>, "end": <now+30 min>, "note": "deploy self-test, will be cancelled"}`, with `Origin: http://10.188.1.1:8765`, expecting 201.
     - Wait until the start time plus 90 s.
     - `GET /api/now` must show the booking on GPU 5.
     - `POST /api/claims/<id>/cancel`, expecting 200.
     - Paste all outputs.
  4. If Playwright is available, save a screenshot of `http://10.188.1.1:8765` to `/private/tmp/claude-501/-Users-christian-work-ResourceMonitor/febe6c9a-8da2-423a-ab2a-2f4a2b41a6aa/scratchpad/booking-live.png`.
