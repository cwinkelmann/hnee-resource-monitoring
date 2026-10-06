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
| python | `/usr/bin/python3` (3.12.3). **conda is not on `PATH`** over non-interactive ssh — which is what we want, since the tool is stdlib-only. |
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
   ssh cwinkelmann@10.188.1.1 'install -m 600 /dev/null ~/.config/resourcemonitor/env && \
     printf "SLACK_WEBHOOK_URL=%s\n" "$URL" > ~/.config/resourcemonitor/env'
   ```

4. **Dry run first, and read the output.**

   ```bash
   ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -m resourcemonitor once'
   ```

   It prints the Slack payload instead of sending it. Confirm the alerts are ones you
   would have wanted to receive. Leave it dry for a day before step 5 — the cost of a
   noisy first week is that the channel gets muted and the tool becomes useless.

5. **Enable the unit**

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

6. **Verify**

   ```bash
   ssh cwinkelmann@10.188.1.1 'systemctl --user status resourcemonitor --no-pager | head -20'
   ```

## When alerts stop arriving

In this order: `systemctl --user status` (is it running?); `journalctl --user -u
resourcemonitor -n 50` (is it erroring?); check the state file — an incident inside its
cooldown is *supposed* to be silent; confirm `SLACK_WEBHOOK_URL` is still set, since an
expired webhook returns a non-2xx that the tool logs but does not crash on.
