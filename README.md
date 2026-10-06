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
| Python 3.12.3 at `/usr/bin/python3`, conda not on a non-interactive `PATH` | stdlib only; unit hardcodes `/usr/bin/python3` |
| `systemd --user` works, `Linger=yes` | runs as a user service, no root |
| Docker is rootless; `dorian.zwanzig` is UID 1053, outside `cwinkelmann`'s subuid range | **cannot run in a rootless container**: inside, `/proc/<pid>` shows `uid=65534 nobody` for other users, so attribution is destroyed |
| `nvidia-smi` reports PIDs only | owner comes from `/proc` |
| `total_energy_consumption` is not supported | energy is integrated from `power.draw` |
| Idle H100 draws about 66 W; box drew 1,532 W with 2 of 8 busy | idle cost is real, hence the report |

Reach the box with `cwinkelmann@10.188.1.1` (the hostname `carrot` does not resolve).

## Running

```
python3 -m resourcemonitor once              # one pass, prints the payload (dry)
python3 -m resourcemonitor watch --interval 60
python3 -m resourcemonitor report            # energy report from the ledger; no polling
```

Dry-run is the default. Nothing reaches Slack unless `--post` is given, and `--post`
refuses to start without `SLACK_WEBHOOK_URL`. Run dry for at least a day first and read
the payloads. Options: `--policy`, `--state`, `--energy`, `--interval`, `--price` (EUR/kWh).

`report --post` is a manual, human-invoked post and deliberately bypasses the alert cooldown.

## Setup

1. Copy `deploy/policy.example.toml` to `~/.config/resourcemonitor/policy.toml` and edit.
2. Webhook: put `SLACK_WEBHOOK_URL=...` in `~/.config/resourcemonitor/env` (mode 0600).
   The URL is a bearer credential: never commit or log it.
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
