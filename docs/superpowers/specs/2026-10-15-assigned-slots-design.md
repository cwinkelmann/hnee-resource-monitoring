# Assigned slots: a booking always succeeds, possibly later, and moves earlier when it can

Design, 2026-10-15. Supersedes the waitlist sketch of the same day, which assumed a worker
fleet and a separate queue of pending requests. There is no orchestration here: **the job
itself is the client.**

## The model

A job asks for resources when it starts. It always gets an answer it can act on:

- capacity is free now → it gets a booking starting now and runs;
- everything is booked → it gets **the assigned slot**: a real booking in the future, at the
  earliest window that fits;
- if someone releases early, the job's slot **moves earlier** and it starts sooner.

No queue to poll for a maybe, no supervisor, no fleet. One call, one answer, a start time.

## The user stories this serves

From `UserStories.md` (8 GPU, 224 CPU, 1 TB RAM server):

| story | behaviour |
|---|---|
| Alice wants a specific window; Bob already holds 5 cards for 6 h | The booking **fails**, as today — an explicit `start` still answers 409. The error carries `earliest` and `free_now` so her two options, *book later* or *book less*, are in the answer instead of guesswork. |
| Alice needs **4 GPUs uninterrupted** and does not care when | An **atomic group booking**: the earliest instant where all four fit for the whole duration, assigned as a set. Her script waits and starts when the slot opens. Without atomicity she would be assigned cards at four different times and sit holding idle ones. |
| Alice books 40 GiB, 40 GiB remain free; Bob should still run | Already true: `_check_capacity` sums VRAM per card, so partial-card sharing works. Pinned by an explicit test so it stays true. |

**CPU and RAM are deliberately not bookable.** PR #6 monitors them for display. With 224
cores and 1 TB against 8 cards, the GPU is the binding constraint; booking cores and RAM
would widen the schema, the capacity check, the form and the SDK for little gain.

## Why

**You cannot wait in line today.** Someone holding eight cards at 10 GiB each blocks an
80 GiB job completely: `_check_capacity` correctly answers 409 and the person who needs the
card has nothing to do but refresh the dashboard and race for it. In the user's words: *"a
potential job just tries to book resources, if the hrm grants the resources the job will
continue … when resources are released beforehand the job can start earlier."*

**You cannot give a card back honestly.** `cancel()` marks the whole booking cancelled
(`claims.py:327`), so a booking used 09:00–09:40 and then cancelled vanishes from the
calendar as if it never existed, and `gpu-energy-report` under-attributes whoever was tidy.
The incentive runs backwards: letting a booking rot scores better than releasing it.

The workload that makes both urgent: **13,000 jobs × ~1 h × ~19.5 GiB** (4 slots per 80 GB
card, 32 on carrot, ~17 days of work).

## What this design does *not* add

- **No orchestrator, supervisor, worker pool or queue runner.** Rejected by the user.
- **No separate `requests` table.** The assigned slot *is* a booking. This is the central
  simplification and the reason the rest is small (see below).
- **No authentication.** Honour system, like the rest of the server. `important` is a claim
  about need, enforced socially and by being visible.
- **No background process.** Nothing new runs; see "When a slot moves earlier".
- **No cross-host queue.** t14 (192.168.188.88) cannot reach carrot (10.188.1.x), so it is
  its own booking domain. One queue across hosts is a separate design.
- **Not a replacement for takes.** The `taken`/grace path (PR #5) remains for a job that
  must die now. Assigned slots are the consensual path and remove the need to take anything
  for the 13,000-job workload.

## Why there is no queue table

A pending request would need its own table, because every consumer of a claims row assumes
a window exists: `active_at`, `_check_capacity`, `load_active`, `list_window`, the calendar
and the energy attribution all read `start`/`end`. Five consumers would have to learn to
skip window-less rows, and the first one missed books over somebody.

Assigning the slot immediately removes the problem instead of solving it. A future booking
is an ordinary claims row, so:

- `_check_capacity` already protects it — **no new restraint rule is needed.** Nobody can
  book VRAM that an assigned slot holds, because it is simply booked. The "no new booking
  may delay a queued request" machinery from the earlier sketch disappears entirely.
- the dashboard already draws it;
- history and energy already treat it correctly;
- `cancel` and `release` already work on it.

## Data model

Schema **v3 → v4** adds two columns to `claims`, following the existing `_migrate()`
pattern on main (`PRAGMA user_version`, one transaction, additive only):

```sql
ALTER TABLE claims ADD COLUMN auto_advance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN group_id     TEXT;   -- NULL for a single-card booking
```

Main is already at `SCHEMA_VERSION = 3`: v1 → v2 added `kind`, v2 → v3 added `priority`.
This is the fourth step and the same shape as the previous two.

`auto_advance = 1` means "if capacity frees earlier, move this booking earlier". It must be
stored, because the move happens later, during someone else's release.

Everything else the feature needs is already on the row on main: `user`, `gpu`, `vram_mib`,
`start`, `end`, `kind`, and `priority` with `PRIORITIES = ("lendable", "important")`.
`allocate()` already derives what an important booking takes from a lendable one without
storing it. There is also prior art for the server choosing a start: `create()` takes a
`grace` and moves a booking's start forward when it would land on a running job. An assigned
slot is the same idea carried further — the server already decides start times.

`group_id` makes a multi-card need atomic. `book(count=4)` creates four ordinary claims
rows sharing one `group_id`, so every existing consumer — `_check_capacity`, `allocate()`,
the calendar, the energy attribution — keeps working unchanged on rows it already
understands. The group matters only to three operations: assignment, advancing and cancel.

A booking is **advanceable** when `auto_advance = 1 AND start > now AND cancelled_at IS NULL`.
A group is advanceable only when **every** member is.

## Booking: `earliest_fit`

New pure module `resourcemonitor/slots.py` — no SQL, no clock of its own:

```python
def earliest_fit(bookings, gpu, vram_mib, duration_s, card_mib, now) -> datetime | None
def earliest_fit_group(bookings, count, vram_mib, duration_s, cards, now,
                       gpus=None) -> tuple[datetime, list[int]] | None
def advance_order(bookings) -> list[Booking]
```

`earliest_fit` builds the step function of booked VRAM on a card from active and future
bookings, and returns the first instant `t >= now` where `booked(c, τ) + vram_mib <=
card_mib` holds for **all** of `[t, t + duration_s)`. With `gpu = None` it is the minimum
across cards; the winning card is the argmin, ties broken by lowest index.

`POST /api/claims` without an explicit `start` now means "earliest": the server answers with
the assigned window rather than 409. An explicit `start` keeps today's exact behaviour,
including 409 — a calendar booking for Tuesday must still fail if Tuesday is full.

`earliest_fit_group` answers the atomic case. It walks the same candidate instants (every
booking start and end at or after `now`, plus `now` itself) and, at each, counts how many
cards can host `vram_mib` for the whole of `[t, t + duration_s)`. The first instant where
that count reaches `count` wins, and the lowest-index fitting cards are chosen. A group is
therefore never assigned staggered start times, which is the whole point of the story.

A 409 for an explicit `start` now carries both `earliest` (when it would fit) and `free_now`
(how much would fit immediately), so "book later or book less" is in the answer.

`not_later_than` is an optional request parameter, not a stored field: if
`earliest_fit > not_later_than` the server answers `409 conflict` with the earliest it could
offer, so a job that cannot wait that long fails fast instead of sleeping for a fortnight.

## Release: truncate, don't delete

One caller-facing verb; the server picks by `start`:

| booking state | effect |
|---|---|
| already started (`start <= now`) | `UPDATE claims SET end = ?` — the window actually used stays on the calendar and in the energy ledger |
| not yet started (`start > now`) | falls back to `cancel` — truncating would leave `end < start`, a nonsense row |

Moving `end` **earlier** always succeeds; it only frees capacity. Moving it **later** is an
extension: it re-runs `_check_capacity` over `[old_end, new_end]` and may answer 409, since
someone may hold the gap — including an assigned slot. Releasing an already-released booking
to `now` is a no-op; releasing a cancelled one answers `409 already cancelled`.

## When a slot moves earlier

Advancing is a pure function of `(bookings, now)` and runs **only inside the transaction of
a write that frees capacity** — `release` or `cancel`.

It needs to run nowhere else, and that is worth stating precisely: **time passing can never
make a slot advanceable.** `earliest_fit` already priced in every known end when the slot
was assigned, so a booking ending on schedule opens no gap that was not already accounted
for. Only an *early* release or a cancel creates one, and both are writes. There is no timer
thread, no lazy-on-read path, and no cron.

Within the releasing transaction:

1. collect advanceable bookings, ordered by `advance_order`: `important` before `lendable`,
   then `start` ascending, then `created_at` ascending (first asked, first moved);
2. for each in turn, recompute `earliest_fit` for its own `vram_mib` and duration on its own
   card (or any card, if it was booked with `gpu = None`); for a group, recompute
   `earliest_fit_group` for the whole shape instead;
3. if that instant is earlier than its current `start`, move the window, preserving
   duration, and record it. **A group moves only if every member can move to the same new
   start** — never partially. A not-yet-started booking may land on a different card than
   it was first assigned, which is harmless because nothing has run on it yet;
4. re-evaluate the next one against the updated timeline.

`POST /api/claims/{id}/release` returns `advanced: [{id, user, old_start, new_start}]`, so
the releaser is told who benefited — the social feedback that makes releasing worth doing.

A moved booking keeps its id, so a sleeping job's poll sees a new `start` on the row it
already knows.

## The queue-colonisation problem

This is the one place the design can go wrong, and it needs a decision.

If a job books on startup and 13,000 job processes start at once, they each take an assigned
slot and together they own the calendar for ~17 days. The 80 GiB person who shows up at hour
two is offered a slot in two weeks — the exact failure this feature exists to prevent.

Proposed: a policy limit on **not-yet-started** bookings per user, in
`~/.config/resourcemonitor/policy.toml`, not in code:

```toml
[booking]
max_pending_per_user = 16      # future bookings that have not started yet
```

Over the limit, `POST /api/claims` answers `409 conflict`, detail `"you already hold 16
bookings that have not started; wait for one to start or cancel one"`. A job that gets that
answer retries later — it is a polite "not yet", not a failure. With 32 cards' worth of
slots and a limit of 16, one user can keep a deep pipeline without owning the future.

This keeps the honour system honest by arithmetic rather than by authentication.

## HTTP surface

Writes count against the existing per-IP budget (1000/h, in memory, resets with the
process — a runaway-loop backstop, not a quota).

| route | request | response |
|---|---|---|
| `POST /api/claims` | existing body, plus optional `auto_advance`, `duration_minutes` (instead of `end`), `not_later_than`; `start` omitted means earliest | `201 {booking}` with the assigned `start`/`end`, and `assigned_later: true` when it does not start now |
| `POST /api/claims/{id}/release` | `{end: "now" \| ISO-8601}` | `200 {booking, advanced:[{id, user, old_start, new_start}]}` |
| `GET /api/capacity` | `?gpu=&vram_mib=&duration_minutes=` | `{free_mib, earliest, gpu}` — the one authoritative capacity answer |

Errors keep the `{"error","detail"}` shape and the fixed messages: `400 invalid`,
`404 not found`, `409 conflict`.

`GET /api/capacity` exists because clients were re-implementing the VRAM sum and got it
wrong: the SDK's own-booking reuse counted a *second* process's 19.5 GiB under the same
account as "already yours", so two jobs would have shared one allocation. The server already
sums correctly. Clients should ask, not compute.

## SDK (`hrm`)

```python
booking = hrm.book(vram_gib=19.5, hours=1, gpu=None,
                   auto_advance=True, not_later_than="4h")   # never raises NoCapacity
booking = booking.wait_until_start()      # sleeps; re-reads, so an advance shortens the wait
...                                        # the job runs
hrm.release(booking)                       # end = now
```

- `book()` returns a `Booking` whose `start` may be in the future; `assigned_later` says so.
- `wait_until_start(poll=10)` polls the row and returns when `start <= now`; it returns
  early when the slot advances, and raises if the booking was cancelled under it.
- `hrm.release(booking, end=None)` truncates; `hrm.cancel` keeps its meaning for a booking
  that has not started.
- `Booking.taken` and `priority` round out the gaps so the SDK can express the
  lendable/important split at all.

### What a job looks like

```python
booking = hrm.book(vram_gib=19.5, hours=1, auto_advance=True)
booking.wait_until_start()
try:
    train()
finally:
    hrm.release(booking)          # ends the booking at the real finish time
```

That is the whole integration. 13,000 of these run as 13,000 ordinary jobs, each holding one
slot at a time; the `max_pending_per_user` limit keeps the calendar shared. Two writes per
job ≈ 64/h at 32 concurrent jobs — impossible under the old 30/h limit, comfortable under
1000/h.

The deliberate cost: a job that finishes does **not** get its slot back automatically. Its
successor books afresh and lands behind anyone waiting. That is the only reason the 80 GiB
job ever gets in, and it needs no enforcement beyond the calendar.

## Dashboard

Future bookings already appear on the calendar. Two additions:

- an **assigned slot** badge on not-yet-started bookings with `auto_advance`, showing
  *"starts 15:00, earlier if a card frees"*;
- on release, a line naming whose slot moved earlier, so the effect of releasing is visible.

On an honour system, visibility is the enforcement mechanism: people yield to a queue they
can see themselves blocking.

## Testing

- `tests/test_slots.py` — table-driven, pure, no DB, no GPU: `earliest_fit` (gaps,
  exact-fit boundaries, no fit before the horizon, floating across cards, `gpu=None`
  argmin and tie-break); `earliest_fit_group` (four cards free at different times → the
  group waits for the fourth; never staggered; lowest-index cards chosen; `count` larger
  than the box → None); and `advance_order` (important first, then start, then created_at).
- Partial-card sharing (the third story): 40 GiB booked on the last free card, a second
  user books the remaining 40 GiB and succeeds; 40 GiB + 41 GiB answers 409.
- Groups: a group never advances partially; cancelling a not-yet-started member cancels the
  set; releasing an under-way member truncates only that row.
- `tests/test_claims_advance.py` — an early release pulls the next slot earlier inside the
  same transaction; a natural end does **not** (nothing to advance); duration is preserved;
  an advanced booking keeps its id; cascading advances re-evaluate against the updated
  timeline; a cancelled booking never advances.
- `tests/test_claims_release.py` — truncate vs cancel by `start`; extension re-checks
  capacity and can 409 against an assigned slot; release idempotent; release of a cancelled
  booking.
- `tests/test_web_claims.py` additions — `start` omitted means earliest; `assigned_later`;
  `not_later_than` → 409 with the earliest offer; `max_pending_per_user` → 409; `advanced:[]`
  on release.
- Migration: a real v3 `claims.sqlite` fixture opened by v4 code gains `auto_advance`,
  keeps every row, and sets `PRAGMA user_version = 4`; a v1 file still migrates all the way.
- Fairness integration: 32 one-hour slots booked by one user, then an 80 GiB request from
  another; assert it is offered the first boundary rather than the end of the fortnight, and
  that it advances when a slot is released early.

## Rollout

Constrained by a fact that will not change: **there is no sudo on carrot or olive, ever.**

1. **Olive** first (2 × A6000; 4 slots at 19.5 GiB — enough for correctness, useless for
   throughput). `Linger=no` with no root means systemd user units are unusable there, so
   olive runs `deploy/resourcemonitor-guard.sh` from cron permanently. That is its supported
   path, not a stopgap.
2. **Carrot** second: it has `Linger=yes` already, so it takes the real units. It needs
   `~/.config/resourcemonitor/host.env` with `RESOURCEMONITOR_BIND=10.188.1.1` before the
   new units start, or the dashboard comes back on loopback only.
3. **t14** third, as its own booking domain for jobs that fit 16 GiB. None of the
   13,000 × 19.5 GiB jobs fit there.

Schema and API are additive: an old SDK keeps working (no `auto_advance`, explicit `start`),
and a v1/v2/v3 database migrates on first write.

**Branch note.** This must be implemented on a branch off `origin/main`. The current
`docs/feature-backlog` checkout predates PRs #5 and #6 — it has no `priority` and no
`allocate()` — so building here would silently reinvent the lendable/important split. The
host-config work (`run-resourcemonitor.sh`, `host.env`) is in the same position and should
move to its own branch off main too.

## Known gaps, recorded deliberately

- **A job that dies while waiting leaves its slot booked.** Nothing reaps it. The existing
  idle rules (`idle_util_pct`, `idle_grace_s`) already alert on a booked-but-idle card once
  the window starts; auto-truncating such a booking is a policy change, deliberately not
  proposed here.
- A suspended laptop's bookings keep ticking while the job sleeps.
- Nothing stops a user from marking everything `important`. Visible calendar, social fix.
- `max_pending_per_user` is a blunt instrument: it limits pipeline depth, not total share.
