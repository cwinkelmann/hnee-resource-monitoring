# GPU Booking — Design

**Date:** 2026-10-06 · **Status:** approved in chat, section by section · **Builds on:** v1 monitor (`docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md`) and the history + LAN dashboard (`docs/superpowers/plans/2026-10-06-history-dashboard.md`), whose Global Constraints still bind unless this document explicitly overrides them.

## Intent

**What the user asked for:** "add the function that a user can assign GPUs to themselves, including VRAM."

**Agreed meaning:**

- People **book a share of a card**: a GPU, an amount of VRAM, and a time window. Two people can share one 80 GB H100.
- Bookings **replace the fixed 0–3 / 4–7 split** for alerting.
- Bookings are made in a **web form on the dashboard, on the honour system**: there is no login, so anyone on the LAN can book or cancel in anyone's name.
- **An unbooked GPU, or the unbooked remainder of a card, is free for anyone.** Alerts fire only when someone uses capacity another person booked, or exceeds their own booking.

**Success:** someone opens `http://10.188.1.1:8765`, books "GPU 4, 40 GiB, until Friday 18:00" in a few clicks, and everyone sees it. If someone else then uses that share, the page shows it in red (and Slack does too, once posting is enabled).

## Decisions the user made explicitly

| decision | consequence accepted |
|---|---|
| Reverse v1's "reserving GPUs is out of scope" | The tool now records reservations. It still never **enforces** them: no kill, no signal, no scheduling. |
| Web form, honour system (rejected: ssh command, per-user tokens) | Anyone on the LAN can book or cancel in any name. The counterweight is a complete audit trail (time and client IP of every booking and cancellation) shown on the page. |
| Unbooked capacity is free | No "unbooked use" nagging. Booking is how you protect capacity. |
| Start clean at switch-over (rejected: seeding from the split) | No bookings exist at launch, so every GPU is free and today's "outside allocation" alerts (andre on 0–3, dorian on 6–7) disappear. |

## Constraints (in addition to the inherited ones)

- **Stdlib only.** Never touch, signal or reprioritise a process. The monitor reports booking violations and nothing more.
- **Write isolation.** Only the web process writes, and it writes only `~/.local/state/resourcemonitor/claims.sqlite`. `history.sqlite` stays `mode=ro` for the web process. `watch` opens `claims.sqlite` with `mode=ro`.
- **The web process still never imports `probe`, `notify` or `cli`.**
- **A missing, corrupt or locked `claims.sqlite` never breaks monitoring.** In that case there are no active bookings, so everything is free, and the error is printed by exception class only.
- **Rows are never deleted.** Cancellation is a soft delete, and the claims table is the audit log.
- **Timestamps** are stored as UTC ISO-8601. The browser sends local time with its offset; input without an offset is rejected.
- Commits carry **no** Co-Authored-By or AI-attribution trailer.

## Section 1: data model and rules

### `claims.sqlite`

```sql
CREATE TABLE IF NOT EXISTS claims (
  id           INTEGER PRIMARY KEY,
  user         TEXT    NOT NULL,
  gpu          INTEGER NOT NULL CHECK (gpu BETWEEN 0 AND 7),
  vram_mib     INTEGER NOT NULL CHECK (vram_mib > 0),
  start        TEXT    NOT NULL,          -- UTC ISO
  end          TEXT    NOT NULL,          -- UTC ISO, end > start
  note         TEXT,                      -- <= 120 chars
  created_at   TEXT    NOT NULL,
  created_ip   TEXT    NOT NULL,
  cancelled_at TEXT,
  cancelled_ip TEXT
);
CREATE INDEX IF NOT EXISTS claims_gpu_window ON claims(gpu, start, end);
PRAGMA user_version = 1;   -- WAL mode
```

A booking is **active at time t** when `cancelled_at IS NULL AND start <= t < end`. There is **no edit operation**: you cancel and rebook.

### Validation (`ClaimsStore.create`)

| rule | error detail (fixed text, never echoed input except validated values) |
|---|---|
| `user` is a real login account on carrot: `pwd` entry with UID ≥ 1000 and a valid name pattern. The lookup is injectable for tests. | `unknown user '<name>'`, printed only if the name matches `^[a-z_][a-z0-9._-]{0,31}$`; otherwise `invalid user name` |
| `gpu` in 0..7 | `GPU must be 0–7` |
| `1 GiB <= vram <= card total`. The card total is `total_mib` from the latest history poll, with a fallback of 81559. | `VRAM must be between 1 and <N> GiB` |
| `start >= now - 5 min` | `start lies in the past` |
| `end > start` | `end must be after start` |
| `end - start <= 14 days` | `a booking may last at most 14 days` |
| `note` ≤ 120 characters, no control characters | `note too long` / `invalid note` |
| **Capacity:** at every instant of `[start, end)`, the VRAM of active overlapping bookings on that GPU plus the new booking must be ≤ the card total. It is checked at each boundary point of the overlapping bookings. | `GPU <n> has only <x> GiB unbooked between <t1> and <t2>` (**409**) |

The capacity check and the insert run in **one `BEGIN IMMEDIATE` transaction** (busy timeout 5 s), so two concurrent bookings cannot both take the last free share.

### Rules

These replace `check_allocation`. The capacity, idle and unattributed rules are unchanged. For each poll, for each GPU:

1. Let `B` be the bookings active at `snap.taken_at` on this GPU. If `B` is empty, the GPU is **free** and no booking alert fires.
2. Sum each user's VRAM on the GPU across that user's processes. Processes with user `None` are handled by the unattributed rule, not here.
3. If a user has a booking `b`, they are within it while `used <= 1.10 × b.vram`. Above that, raise **`over_booking`**: key `booking:over:<user>:<gpu>`, text "`<user>` uses `<X>` GiB on GPU `<n>` but booked `<Y>` GiB."
4. Users **without** a booking share the **unbooked remainder** `R = total − Σ b.vram`. If their combined use exceeds `R`, raise **`booked_gpu`** for each such user: key `booking:other:<user>:<gpu>`, text "`<user>` is using `<X>` GiB on GPU `<n>`; `<Σ>` GiB is booked by `<users>` until `<earliest end>`, `<R>` GiB unbooked."
5. Keys are stable per (kind, user, GPU), so the existing cooldown and `state.should_send` apply unchanged.

`policy.toml` `[assignments]` becomes **optional** and is ignored by the rules. It may remain in existing files without error.

## Section 2: API (write path)

| route | method | behaviour |
|---|---|---|
| `/api/claims?days=14` | GET | Bookings that are active now, upcoming (up to `days` ahead, 1..14) or recent (7 days back), including cancelled ones with their audit fields. |
| `/api/claims` | POST | Body JSON `{"user", "gpu", "vram_gib", "start", "end", "note"?}`. Returns **201** with the stored booking. |
| `/api/claims/<id>/cancel` | POST | Sets `cancelled_at` and `cancelled_ip`. Returns **200**. Cancelling an already-cancelled booking returns 200 with no change. Unknown id returns **404**. |
| `/api/users` | GET | Login accounts on carrot (UID ≥ 1000, sorted), for the form. |

- **Errors:** 400 `{"error": "invalid", "detail": <fixed text>}`, 409 `{"error": "conflict", "detail": …}`, 404 for unknown id, **403** for a foreign `Origin`, **415** for non-`application/json`, **413** for a body over 4 KB, **429** when a client IP exceeds 30 writes per hour (in-memory sliding window).
- **Drive-by protection:**
  - POSTs require `Content-Type: application/json`, which forces a CORS preflight that we never answer.
  - If an `Origin` header is present, it must equal the request's own `http://<Host>`.
  - No CORS headers are ever sent.
- Every response keeps CSP `default-src 'self'`, `nosniff` and `no-store`.
- The fixed-route list in the dashboard plan's Global Constraints gains these four routes. The 405 set becomes "any method other than GET, plus POST on the two POST routes".
- **Code:**
  - `resourcemonitor/claims.py` is new: `ClaimsStore(path, user_lookup=…)` with `create`, `cancel`, `active_at(t)`, `list_window(now, days_ahead, days_back)`, plus `open_claims_ro(path)` for `watch`.
  - `web.py` routes to it.
  - `rules.py` gains `check_bookings(snap, pol, bookings)`.
  - `cli.run_once` loads active bookings (read-only, fail-open) and calls `check_bookings` in place of `check_allocation`.

## Section 3: the page

- **A new "Bookings" section, directly under Now:**
  - **Calendar:** one row per GPU, from now to +14 days. Each booking is a bar in the user's colour with `user · NN GiB` (and the note if it fits). Bar **height is proportional to the booked share**, so shared cards show stacked bars, and empty space is free capacity.
  - **Clicking a bar** shows its details: user, VRAM, window, note, and booked at / from IP (plus cancelled at / from IP). A **Cancel** button needs an in-page two-step confirmation, because no `confirm()` is available.
  - **"Book a GPU" form:**
    - user: a select filled from `/api/users`; the last choice is kept in `localStorage`, wrapped in try/catch;
    - GPU 0–7: each option shows the free VRAM in the chosen window;
    - VRAM in GiB, capped at free capacity;
    - from: defaults to now;
    - until: quick buttons for **+4 h, +1 day, Friday 18:00 and custom**;
    - an optional note;
    - a live preview, e.g. "you'd book 40 of 79.6 GiB; 39.6 GiB stays free";
    - server error details shown inline.
  - **Recent changes:** the last 20 bookings and cancellations, with time, action, GPU, VRAM, user, window and client IP.
- **Now cards:** "assigned to X" is replaced by the active bookings and the free VRAM. The VRAM bar shows booked shares as outlined segments with actual usage filled on top. A card turns red only for a `booked_gpu` or `over_booking` alert; the "outside allocation" tag is removed.
- **Timeline:** past bookings appear as faint bands behind the job bars. A job bar is outlined red when its user had **no** booking on that GPU while **someone else's** booking was active for part of the job's run. This is a display hint only. The VRAM-precise judgement belongs to the live `booked_gpu` alert.
- **Unchanged:** Energy, Last 24 h, and the safety rules: `textContent`/`setAttribute` only, no inline script or style, no external URLs.

## Section 4: testing and rollout

### Tests (TDD)

- **`claims.py`:**
  - every validation row;
  - capacity at the worst instant: three bookings overlapping only part-way;
  - **a real two-thread race**, where exactly one booking succeeds and the other gets 409;
  - cancel and cancelling twice;
  - `active_at` boundaries (start inclusive, end exclusive);
  - `list_window`.
- **Rules:**
  - free GPU; within share; exactly 110 % (no alert) and above it;
  - the remainder shared by several users without bookings;
  - several bookings on one card;
  - expired and cancelled bookings ignored;
  - **a missing or corrupt claims DB gives no booking alerts while other rules still fire.**
- **Web:**
  - 201, 400, 409, 404, 403, 415, 413 and 429 with exact bodies;
  - headers on every response;
  - re-check that the history DB is unwritable from the web process;
  - re-check that `serve` does not load `probe`, `notify` or `cli`.
- **Page:**
  - static tests;
  - a Node test of the pure free-VRAM / overlap function;
  - a local Playwright run that books through the form, sees the booking in the calendar, and cancels it.
- **Carrot, end to end:** book through the live page, confirm the Now card shows the booking within one poll, then cancel it.

### Rollout

- Branch `feat/gpu-booking` (stacked on `feat/history-dashboard`) becomes PR #3.
- rsync, then restart `resourcemonitor-soak` and `resourcemonitor-web`. `claims.sqlite` is created on the first booking. Slack stays dry.
- **Start clean:** no seeded bookings.
- Docs:
  - README gets "Booking a GPU", explicit about the honour system and that nothing is enforced.
  - CLAUDE.md gets: "the web process writes only claims.sqlite; history stays read-only".
  - The deploy skill notes the new file.
  - The v1 plan's "Deferred: Reserving/queueing GPUs" gets a pointer to this spec.

## Out of scope

Recurring bookings, editing, per-user quotas, waiting lists, enforcement of any kind, notifications to the booked user, drag-to-book, real authentication.
