"""`once` for a single pass, `watch` for the service. Dry-run unless --post."""
from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import time
from pathlib import Path

from resourcemonitor.energy import EnergyLedger, format_report
from resourcemonitor.history import HistoryWriter
from resourcemonitor.model import Snapshot
from resourcemonitor.notify import Notifier
from resourcemonitor.paths import DEFAULT_ENERGY, DEFAULT_HISTORY, DEFAULT_POLICY, DEFAULT_STATE
from resourcemonitor.policy import load_policy
from resourcemonitor.probe import probe
from resourcemonitor.rules import (Alert, IdleTracker, check_allocation, check_capacity,
                                    check_unattributed)
from resourcemonitor.state import State


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resourcemonitor")
    p.add_argument("mode", choices=["once", "watch", "report", "serve"])
    p.add_argument("--post", action="store_true",
                   help="actually post to Slack (default: print the payload only)")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--energy", type=Path, default=DEFAULT_ENERGY)
    p.add_argument("--price", type=float, default=0.30,
                   help="EUR per kWh, used only to annotate the report")
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                   help="SQLite history file (watch only)")
    p.add_argument("--bind", default="127.0.0.1", help="serve: address to listen on")
    p.add_argument("--port", type=int, default=8765, help="serve: TCP port")
    p.add_argument("--stale-after", type=int, default=180,
                   help="serve: seconds without a poll before the page shows 'stale'")
    p.add_argument("--no-history", action="store_true", help="do not record history")
    p.add_argument("--retention-days", type=int, default=90,
                   help="history older than this is pruned")
    return p


def run_once(pol, state, notifier, tracker, ledger, host, energy_path,
             history=None) -> int:
    snap = probe()
    dt_s, rows = ledger.accumulate(snap)   # before the rules: a poll always costs energy
    ledger.save(energy_path)
    alerts = check_allocation(snap, pol) + check_capacity(snap, pol) \
        + check_unattributed(snap, pol) \
        + tracker.observe(snap, pol)
    fresh = [a for a in alerts if state.should_send(a.key, snap.taken_at, pol.cooldown_s)]
    delivered = notifier.send(fresh, host) if fresh else False
    state.save()
    # "sent" in history means it reached Slack: never in a dry run, never on a failed post.
    sent_keys = {a.key for a in fresh} if delivered and not notifier.dry_run else set()
    if history is not None:
        try:
            history.record(snap, dt_s, rows, alerts, sent_keys)
        except (sqlite3.Error, OSError) as e:
            # class name only: the message may embed paths
            print(f"history write failed: {e.__class__.__name__}", flush=True)
    return len(fresh)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "serve":
        # Kept for help/compat. `python -m resourcemonitor serve` never reaches here: it is
        # dispatched to web.main before this module (and probe/notify) is imported.
        from resourcemonitor import web
        return web.main(["--bind", args.bind, "--port", str(args.port),
                         "--history", str(args.history), "--policy", str(args.policy),
                         "--stale-after", str(args.stale_after)])
    pol = load_policy(args.policy)
    state = State.load(args.state)
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if args.post and not url:
        raise SystemExit("--post given but SLACK_WEBHOOK_URL is not set")
    if args.post and not url.startswith("https://"):
        raise SystemExit("SLACK_WEBHOOK_URL must start with https://")
    notifier = Notifier(url, dry_run=not args.post)
    tracker = IdleTracker()
    ledger = EnergyLedger.load(args.energy, max_gap_s=args.interval * 5)
    host = socket.gethostname()

    if args.mode == "report":
        # Reads the ledger only; it does not poll, so it is safe to run any time.
        # A manual, human-invoked post: deliberately not routed through the cooldown.
        text = format_report(EnergyLedger.load(args.energy).totals(), args.price)
        if args.post:
            notifier.send([Alert(kind="report", key="report:manual", text=text)], host)
        else:
            print(text)
        return 0

    if args.mode == "once":
        run_once(pol, state, notifier, tracker, ledger, host, args.energy)
        return 0

    history = None                   # watch only: once/report never write history
    if not args.no_history:
        try:
            history = HistoryWriter(args.history, args.retention_days,
                                    slack_mode="dry-run" if notifier.dry_run else "posting")
        except (sqlite3.Error, OSError) as e:
            print(f"history disabled: {e.__class__.__name__}", flush=True)

    while True:                      # watch
        try:
            run_once(pol, state, notifier, tracker, ledger, host, args.energy,
                     history=history)
        except Exception as e:       # a bad poll must not end the service
            # class name only: the message may embed the webhook URL
            print(f"poll failed: {e.__class__.__name__}", flush=True)
        time.sleep(args.interval)
