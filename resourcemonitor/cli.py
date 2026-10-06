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
from resourcemonitor.policy import load_policy
from resourcemonitor.probe import probe
from resourcemonitor.rules import (Alert, IdleTracker, check_allocation, check_capacity,
                                    check_unattributed)
from resourcemonitor.state import State

DEFAULT_POLICY = Path.home() / ".config/resourcemonitor/policy.toml"
DEFAULT_STATE = Path.home() / ".local/state/resourcemonitor/state.json"
DEFAULT_ENERGY = Path.home() / ".local/state/resourcemonitor/energy.json"
DEFAULT_HISTORY = Path.home() / ".local/state/resourcemonitor/history.sqlite"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resourcemonitor")
    p.add_argument("mode", choices=["once", "watch", "report"])
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
    if fresh:
        notifier.send(fresh, host)
    state.save()
    if history is not None:
        try:
            history.record(snap, dt_s, rows, alerts, {a.key for a in fresh})
        except (sqlite3.Error, OSError) as e:
            # class name only: the message may embed paths
            print(f"history write failed: {e.__class__.__name__}", flush=True)
    return len(fresh)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
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
            history = HistoryWriter(args.history, args.retention_days)
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
