# ResourceMonitor

A small, stdlib-only, **read-only** monitor for the shared 8 x H100 box ("carrot").
It polls `nvidia-smi`, attributes each GPU process to its OS user via `/proc`, and
posts to Slack when the agreed GPU split is violated. **It never kills, signals or
deprioritises anything.** It only observes and reports.

## Rules

- **allocation** - a user runs a process on a GPU the policy does not assign to them.
- **idle** - a process holds GPU memory while the card sits below the utilisation threshold past the grace period (the forgotten notebook).
- **capacity** - no GPU has the configured amount of free memory left.

Energy is also tracked (see Energy below). Unknown owners are reported as unknown, never guessed.

## Measured facts (carrot, 2026-10-06)

| fact | consequence |
|---|---|
| Services run in the `resourcemonitor` conda env (user's choice) | stdlib only; `deploy/run-resourcemonitor.sh` takes that env when it exists and falls back to `python3`, so a host without conda (the t14) still runs. Recreate it with `~/miniconda3/bin/conda create -y -n resourcemonitor python=3.12 pytest` |
| `systemd --user` works, `Linger=yes` | runs as a user service, no root |
| Docker is rootless; `dorian.zwanzig` is UID 1053, outside `cwinkelmann`'s subuid range | **cannot run in a rootless container**: inside, `/proc/<pid>` shows `uid=65534 nobody` for other users, so attribution is destroyed |
| `nvidia-smi` reports PIDs only | owner comes from `/proc` |
| `total_energy_consumption` is not supported | energy is integrated from `power.draw` |
| Idle H100 draws about 66 W; box drew 1,532 W with 2 of 8 busy | idle cost is real, hence the report |

Reach the box with `cwinkelmann@10.188.1.1` (the hostname `carrot` does not resolve).

## Running

```
~/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor once              # one pass, prints the payload (dry)
~/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor watch --interval 60
~/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor report            # energy report from the ledger; no polling
```

Dry-run is the default. Nothing reaches Slack unless `--post` is given, and `--post`
refuses to start without `SLACK_WEBHOOK_URL`. Run dry for at least a day first and read
the payloads. Options: `--policy`, `--state`, `--energy`, `--interval`, `--price` (EUR/kWh).

`report --post` is a manual, human-invoked post and deliberately bypasses the alert cooldown.

## Setup

1. Copy `deploy/host.env.example` to `~/.config/resourcemonitor/host.env` and set this
   machine's bind address (`carrot` 10.188.1.1, `olive` 10.188.2.1, `t14` 192.168.188.88).
   Without it the dashboard binds 127.0.0.1 only, so a new host is never exposed by accident.
   The units are host-agnostic: they all start `deploy/run-resourcemonitor.sh`, which reads
   this file. systemd cannot take `ExecStart`'s executable from a variable, which is why the
   wrapper exists at all.
1. Copy `deploy/policy.example.toml` to `~/.config/resourcemonitor/policy.toml` and edit.
2. Webhook: put `SLACK_WEBHOOK_URL=...` in `~/.config/resourcemonitor/env` (mode 0600).
   The URL is a bearer credential: never commit or log it, and never put it in a command line
   (argv is visible via `ps`, history keeps it); write the file from an editor or pipe it over stdin.
3. Install the unit: copy `deploy/resourcemonitor.service` to `~/.config/systemd/user/`, then
   `systemctl --user daemon-reload && systemctl --user enable --now resourcemonitor`.

The unit deliberately omits `ProtectSystem`, `ProtectHome` and `PrivateTmp`. In a systemd
user unit these imply `PrivateUsers=yes`, a user namespace that rewrites every foreign UID
in `/proc` to 65534 - the same attribution failure as the container. `ProtectHome=read-only`
would also make `~/.local/state` unwritable. `NoNewPrivileges=true` is kept.

## Energy

Figures are `sum(power.draw x dt)` over the monitor's own polls, so they cover **only the
time the monitor was running**, not a complete meter reading. Gaps longer than 5 intervals
are not integrated. `power.draw` is per card; the per-user split is a **memory-share
estimate** when several users share a card (exact with a single process), not a measurement.

## Tests

`python3 -m pytest`

## Dashboard

`python -m resourcemonitor serve` serves a read-only "who does what when" page at
**http://10.188.1.1:8765** (unit: `deploy/resourcemonitor-web.service`).

- **Now** — live per-GPU state, refreshed every 30 s.
- **Timeline** — which user ran what on which GPU over time.
- **Energy usage** — kWh per user and per day (UTC days), with idle and unattributed shown separately.
- **Power & utilisation — last 24 h** — power and utilisation per GPU.
- **VRAM — last 24 h** — VRAM per GPU stacked by user, with the booked share as a dashed line (`/api/vram?hours=1..168`).

It is **unauthenticated and visible to the whole LAN by choice**, and strictly **read-only**:
the web process opens the history database with `mode=ro` and never imports probe or notify.

History lives in `~/.local/state/resourcemonitor/history.sqlite`, written by `watch` on every
poll (default on) and pruned to 90 days (`--retention-days`).

Energy caveats apply to every kWh figure: measured only while the monitor was running, and
per-user kWh splits a card's draw by memory share when several processes share it, so it is an
estimate, not a measurement.

## Booking a GPU

Book a share of a GPU on the dashboard (VRAM for a time window): pick your user, the GPU, how
many GiB (1 GiB up to the card total), a start and an end, and an optional note (120
characters max). A booking lasts at most **14 days**. To cancel, use the cancel button on your
booking; cancelling keeps the row (marked cancelled), nothing is deleted. Leaving VRAM empty
books the whole card.

**Quick booking.** Each GPU card in *Now* has a `holder` dropdown. Picking a name books the whole
card for that user from now until the next **09:00** (Europe/Berlin), so every morning the
cards are free again; picking `— free —` releases it. The dropdown only works on a card with no
active calendar booking (then it shows "booked — use the calendar", or "partly booked — use
the calendar" when the bookings leave part of the card unbooked), and a quick booking
ends early where a calendar booking on that GPU begins. It never cancels a calendar booking.

**Lendable vs important.** A booking is *lendable* unless you tick **Important**; quick holds
are always lendable. At any moment, important bookings get their full VRAM and the rest of the
card goes to lendable bookings oldest first, so the newest lendable booking shrinks first.
An important booking therefore only has to fit beside other important bookings: it may take
part of a lendable one, and the form and the reply say whose and how much. If that lendable
booking has already started, the important one starts no earlier than `grace_minutes` (default
30, `[booking]` in `policy.toml`) from when it is made, so the holder gets notice and a `taken`
alert. A lendable booking must fit into what is unbooked. Bookings made before this existed
count as important. Nothing about a take is stored: cancelling the important booking gives the
capacity back.

**Assigned slots.** A booking with no start never fails for lack of capacity: the server
assigns the earliest window that fits and answers with it (`assigned_later: true` when that
is not now). Tick **Earliest slot** on the form and give a length in minutes; tick **Move
earlier if a card frees up** and the slot moves itself forward whenever someone releases
early. `count` books several cards atomically -- one `group_id`, one shared start, so a job
that needs four GPUs never holds idle ones waiting for the fourth. `max_pending_per_user`
(default 16, `[booking]` in `policy.toml`) caps how many not-yet-started bookings one person
may hold, counted in rows, so a long pipeline cannot own the calendar.

**Releasing.** `POST /api/claims/<id>/release` moves a started booking's `end` to now
instead of cancelling it, so the window actually used stays on the calendar and in the
energy ledger; a booking that has not started is cancelled instead. The reply lists the
assigned slots that moved earlier because of it.

**Revocation and the watch channel.** A job holding a lendable booking can declare
`notice_seconds` -- how much warning it needs before giving up the card -- and park a
long-poll on `GET /api/claims/<id>/events`. That request answers nothing until the booking
is taken or cancelled, then returns one JSON event and closes (`204` after 60 s, reconnect).
A declared notice may only ever *shorten* the wait, never lengthen it past `grace_minutes`,
and it counts only while the watcher is actually parked: a crashed job keeps the full grace.
When an important booking starts, a victim left with less than 1 GiB has its `end` truncated
to that instant and the freed tail advances whoever is waiting. `max_watchers` (default 64)
caps the parked connections; past it the route answers 503 and clients fall back to polling.
The server writes rows and notifies clients -- it never signals a process. `deploy/acceptance-slots.py`
drives the whole path against a live server:

```bash
python3 deploy/acceptance-slots.py --base http://10.188.1.1:8765 --user cwinkelmann --gib 20
```

It refuses to start until `watch` has recorded one poll, because without history the server
assumes 81559 MiB cards and the run would prove nothing on a smaller GPU.

This is an **honour system**. The dashboard is unauthenticated, so anyone on the LAN can book
or cancel in any name; every change is shown with its time and IP address. Anyone who can
reach the page can book or cancel, including through other host names or addresses that point
at the server.

Bookings are reported and **never enforced**: no process is ever signalled, limited or
reprioritised. The monitor only compares each user's real VRAM use on a card with what they
booked and alerts on a mismatch:

- Use above **110 %** of the booked VRAM raises an alert (exactly 110 % does not). For a
  lendable booking that is partly taken, this is measured against what it has left.
- Using a card someone booked, beyond its unbooked remainder, raises an alert; unbooked cards
  and unbooked VRAM are free for anyone.
- A quick hold (holder dropdown) blocks calendar bookings on that card until 09:00 unless its
  holder is set back to free.

Bookings live in `~/.local/state/resourcemonitor/claims.sqlite`, written only by the web
process. If that file is missing or broken, monitoring carries on as if nothing were booked.
