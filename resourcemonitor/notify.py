"""Slack incoming webhook. One grouped message per cycle."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from resourcemonitor.rules import Alert

ICON = {"allocation": ":no_entry_sign:", "idle": ":zzz:", "capacity": ":rotating_light:"}


def build_payload(alerts: list[Alert], host: str) -> dict:
    lines = [f"{ICON.get(a.kind, ':grey_question:')} {a.text}" for a in alerts]
    return {
        "text": f"GPU report for {host}",     # fallback for notifications
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"GPU report — {host}"}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }


class Notifier:
    def __init__(self, webhook_url: str, dry_run: bool = True) -> None:
        self._url = webhook_url
        self.dry_run = dry_run

    def __repr__(self) -> str:        # never leak the URL into logs or tracebacks
        return f"Notifier(dry_run={self.dry_run})"

    def send(self, alerts: list[Alert], host: str) -> bool:
        if not alerts:
            return False
        payload = build_payload(alerts, host)
        if self.dry_run:
            print(json.dumps(payload, indent=2))
            return True
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except urllib.error.URLError as e:
            # Slack being down must not kill the daemon. Report and carry on; the
            # cooldown has already been consumed, so this incident waits a cycle.
            print(f"slack post failed: {e.__class__.__name__}")
            return False
