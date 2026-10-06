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
| Services run in the `resourcemonitor` conda env (user's choice) | stdlib only; units hardcode `%h/miniconda3/envs/resourcemonitor/bin/python`. If the env is removed or renamed the monitor stops: `~/miniconda3/bin/conda create -y -n resourcemonitor python=3.12 pytest` |
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

This is an **honour system**. The dashboard is unauthenticated, so anyone on the LAN can book
or cancel in any name; every change is shown with its time and IP address. Anyone who can
reach the page can book or cancel, including through other host names or addresses that point
at the server.

Bookings are reported and **never enforced**: no process is ever signalled, limited or
reprioritised. The monitor only compares each user's real VRAM use on a card with what they
booked and alerts on a mismatch:

- Use above **110 %** of the booked VRAM raises an alert (exactly 110 % does not).
- Using a card someone booked, beyond its unbooked remainder, raises an alert; unbooked cards
  and unbooked VRAM are free for anyone.
- A quick hold (holder dropdown) blocks calendar bookings on that card until 09:00 unless its
  holder is set back to free.

Bookings live in `~/.local/state/resourcemonitor/claims.sqlite`, written only by the web
process. If that file is missing or broken, monitoring carries on as if nothing were booked.
