---
name: deploy-resourcemonitor
description: Use when installing, updating or restarting the GPU ResourceMonitor service on carrot, or when its Slack alerts have stopped arriving. Covers the host-only constraint, the webhook secret, the dry-run soak and the systemd user unit.
---

# Deploying ResourceMonitor

## Reaching the box

| | |
|---|---|
| address | **`cwinkelmann@10.188.1.1`** |
| hostname | **`carrot` does not resolve in DNS** (`SERVFAIL`) — use the IP, always |
| auth | key-based; `ssh -o BatchMode=yes` works with no prompt |
| python | `~/miniconda3/envs/resourcemonitor/bin/python` (3.12). Units run in that conda env by the user's choice; code stays stdlib-only. **If the env is removed or renamed, the monitor stops** — recreate with `~/miniconda3/bin/conda create -y -n resourcemonitor python=3.12 pytest`. |
| hardware | 8 × H100 80GB HBM3, driver 580.178.04, shared with `dorian.zwanzig` |

Quick liveness check before anything else:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 'hostname; nvidia-smi --query-gpu=count --format=csv,noheader | head -1'
```

The service runs as a `systemd --user` unit; `Linger=yes` is already set for this user, so
it survives logout and needs no root.

## Never containerise this

A rootless container rewrites every foreign UID to 65534 (`nobody`), so process
attribution silently breaks while the tool appears to work. Measured 2026-10-06:
`/proc/<pid>/status` reads `Uid: 1053` on the host and `Uid: 65534` in the container.
Asking root for a uid→name table does not help — the UID is destroyed before any lookup.

## Steps

1. **Sync the code**

   ```bash
   rsync -a --exclude .git --exclude __pycache__ ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/
   ```

2. **Policy** — `~/.config/resourcemonitor/policy.toml`, from `deploy/policy.example.toml`.
   The `[assignments]` keys are OS usernames exactly as `/proc` reports them.

3. **Webhook secret** — mode 0600, never committed:

   ```bash
   read -rs URL        # paste the URL here; never type it inline on a command line
   printf 'SLACK_WEBHOOK_URL=%s\n' "$URL" | ssh cwinkelmann@10.188.1.1 \
     'umask 077; mkdir -p ~/.config/resourcemonitor; cat > ~/.config/resourcemonitor/env'
   unset URL
   ```

   The secret travels over stdin, so it never appears in argv (`ps`) or shell history, and
   `$URL` is expanded locally, not inside the remote command.

4. **Dry run first, and read the output.**

   ```bash
   ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && ~/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor once'
   ```

   It prints the Slack payload instead of sending it. Confirm the alerts are ones you
   would have wanted to receive. Leave it dry for a day before step 6 — the cost of a
   noisy first week is that the channel gets muted and the tool becomes useless.

5. **Dry soak (writes history, never posts)** — leave this running while you watch the output:

   ```bash
   ssh cwinkelmann@10.188.1.1 'systemd-run --user --unit=resourcemonitor-soak --working-directory=/home/cwinkelmann/ResourceMonitor -p Restart=always -p RestartSec=30 -E PYTHONUNBUFFERED=1 /home/cwinkelmann/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor watch --interval 60'
   ```

   No `--post`. The dashboard only gets new data while either this soak or the real unit
   is running `watch`: history is written by `watch`, never by `serve`.

6. **Enable the unit**

   Before enabling, stop any dry-run transient unit and remove its state to avoid cooldowns silencing real incidents:

   ```bash
   ssh cwinkelmann@10.188.1.1 'systemctl --user stop resourcemonitor-soak; rm ~/.local/state/resourcemonitor/state.json 2>/dev/null || true'
   ```

   Then enable the service:

   ```bash
   ssh cwinkelmann@10.188.1.1 'cp ~/ResourceMonitor/deploy/resourcemonitor.service \
     ~/.config/systemd/user/ && systemctl --user daemon-reload && \
     systemctl --user enable --now resourcemonitor'
   ```

7. **Verify**

   ```bash
   ssh cwinkelmann@10.188.1.1 'systemctl --user status resourcemonitor --no-pager | head -20'
   ssh cwinkelmann@10.188.1.1 'systemctl --user status resourcemonitor-web --no-pager | head -12'
   ssh cwinkelmann@10.188.1.1 'curl -s http://10.188.1.1:8765/healthz'
   ```

## When alerts stop arriving

In this order: `systemctl --user status` (is it running?); `journalctl --user -u
resourcemonitor -n 50` (is it erroring?); check the state file — an incident inside its
cooldown is *supposed* to be silent; confirm `SLACK_WEBHOOK_URL` is still set, since an
expired webhook returns a non-2xx that the tool logs but does not crash on.

## Dashboard

Read-only web page on the LAN (no auth, by choice), served by `resourcemonitor-web`:

```bash
ssh cwinkelmann@10.188.1.1 'cp ~/ResourceMonitor/deploy/resourcemonitor-web.service \
  ~/.config/systemd/user/ && systemctl --user daemon-reload && \
  systemctl --user enable --now resourcemonitor-web'
ssh cwinkelmann@10.188.1.1 'curl -s http://10.188.1.1:8765/healthz'
```

Then open http://10.188.1.1:8765. Firewall caveat: if `healthz` works on carrot but the
LAN cannot reach port 8765, ask carrot's admin to open it; do not change the firewall yourself.

## Bookings

GPU bookings live in `~/.local/state/resourcemonitor/claims.sqlite`, created and written only
by the web process (`history.sqlite` stays read-only to it; `watch` reads claims read-only).
After every code update restart **both** units, since the soak reads bookings and the web
unit serves them:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 'systemctl --user restart resourcemonitor-soak resourcemonitor-web'
```

The v1 -> v2 schema migration of `claims.sqlite` (adds the `kind` column; existing rows become
`calendar`) happens automatically when the web process first opens the file for a booking
write. There is no manual step. To keep a copy first, note the file is in WAL mode: a plain `cp`
of `claims.sqlite` can miss every row (they may still sit in `claims.sqlite-wal`). Use the
SQLite backup API instead:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 'cd ~/.local/state/resourcemonitor && ~/miniconda3/envs/resourcemonitor/bin/python -c "import sqlite3; s=sqlite3.connect(\"file:claims.sqlite?mode=ro\", uri=True); d=sqlite3.connect(\"claims.sqlite.bak\"); s.backup(d); d.close()"'
```

Pre-check before deploying: quick bookings ("holder" dropdown, until 09:00 Europe/Berlin) need
the IANA time-zone database. Without system tzdata the `/api/claims/quick` route answers 500:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 '~/miniconda3/envs/resourcemonitor/bin/python -c "import zoneinfo; zoneinfo.ZoneInfo(\"Europe/Berlin\")"'
```
