#!/usr/bin/env python3
"""End-to-end acceptance run for assigned slots and revocation, against a live server.

Stdlib only, no GPU work: bookings are bookkeeping, so this proves the booking server
without allocating a byte of VRAM. It plays the part a job's wrapper plays in production --
book, park a watcher, get taken, release -- and is the contract the hrm SDK is written
against. The client shape it mimics is playground/booking_watch_mvp.py, with that spike's
poll loop replaced by the long-poll on /events.

    python3 deploy/acceptance-slots.py --base http://192.168.188.88:8765 \
        --user christian --gib 6

Each step prints PASS or FAIL; the exit status is non-zero if any step failed. Every
booking it makes is released and cancelled before it exits, including on failure.

It refuses to start unless the server reports a plausible card size, because without a
single `watch` poll in the history the server assumes 81559 MiB cards and the run would
book 79 GiB on a 16 GiB laptop GPU and prove nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

TIMEOUT = 15
MIB_PER_GIB = 1024


def call(method: str, url: str, body: dict | None = None, timeout: int = TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": type(e).__name__}


class Run:
    def __init__(self, base: str, user: str, gib: int):
        self.base, self.user, self.gib = base.rstrip("/"), user, gib
        self.failures = 0
        self.made: list[int] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            self.failures += 1
        return ok

    def book(self, **body) -> dict | None:
        d = {"user": self.user, "vram_gib": self.gib, "duration_minutes": 10}
        d.update(body)
        status, out = call("POST", self.base + "/api/claims", d)
        if status != 201:
            print(f"      booking refused: {status} {out}")
            return None
        for c in out["claims"]:
            self.made.append(c["id"])
        return out

    def capacity(self, **q) -> dict:
        query = "&".join(f"{k}={v}" for k, v in q.items())
        status, out = call("GET", f"{self.base}/api/capacity?{query}")
        return out if status == 200 else {}

    def watch(self, claim_id: int, sink: list) -> threading.Thread:
        """Park a long-poll on one booking, as a job's wrapper would. The answer lands in
        `sink` so the caller can assert on it."""
        t = threading.Thread(
            target=lambda: sink.append(
                call("GET", f"{self.base}/api/claims/{claim_id}/events", timeout=90)),
            daemon=True)
        t.start()
        return t

    def cleanup(self) -> None:
        """Release what started and cancel what did not, so a failed run leaves the box as
        it found it. Both calls are tried for every booking; one of them is a no-op."""
        for cid in reversed(self.made):
            call("POST", f"{self.base}/api/claims/{cid}/release", {})
            call("POST", f"{self.base}/api/claims/{cid}/cancel", {})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", required=True, help="e.g. http://192.168.188.88:8765")
    p.add_argument("--user", required=True, help="an OS account on that host")
    p.add_argument("--gib", type=int, default=6,
                   help="VRAM per booking; three of them must not fit on one card")
    args = p.parse_args(argv)
    r = Run(args.base, args.user, args.gib)

    try:
        # 0. the server is there, and it knows the real card size
        status, health = call("GET", r.base + "/healthz")
        r.check("healthz answers", status == 200, json.dumps(health))
        cap = r.capacity(vram_gib=1, duration_minutes=10)
        free = cap.get("free_mib", 0)
        if not r.check("capacity reports a plausible card",
                       0 < free <= args.gib * 3 * MIB_PER_GIB,
                       f"free_mib={free}; is `watch` running? without a poll the server "
                       f"assumes 81559 MiB cards"):
            return 1

        # 1. a booking with no start begins now
        first = r.book(notice_seconds=0, priority="lendable")
        if not r.check("a free card is assigned now",
                       first is not None and first["assigned_later"] is False):
            return 1
        a = first["claim"]

        # 2. a second fits beside it; a third does not and is assigned later
        second = r.book(notice_seconds=0, priority="lendable", gpu=a["gpu"])
        if not r.check("a second booking fits beside the first", second is not None):
            return 1
        # notice_seconds=0 on this one too: a victim with no declared notice keeps the
        # full grace_minutes even with a watcher parked, and step 4 would then prove
        # nothing.
        third = r.book(auto_advance=True, priority="lendable", gpu=a["gpu"],
                       notice_seconds=0)
        r.check("the card is full, so the third is assigned later",
                third is not None and third["assigned_later"] is True,
                "" if third is None else third["claim"]["start"])

        # 3. releasing the first early pulls the third earlier
        status, out = call("POST", f"{r.base}/api/claims/{a['id']}/release", {})
        moved = [m for m in (out or {}).get("advanced", [])
                 if third and m["id"] == third["claim"]["id"]]
        r.check("an early release pulls the assigned slot earlier",
                status == 200 and bool(moved),
                "" if not moved else f"{moved[0]['old_start']} -> {moved[0]['new_start']}")

        # 4. watchers are parked on every booking we hold, then an important booking takes
        # the card. Every running victim needs a live watcher or the important booking
        # waits out the full grace_minutes for the unwatched one -- which is the whole
        # point of the rule, and the commonest reason this step "does nothing".
        victim = second["claim"]
        held = [victim] + ([third["claim"]] if third else [])
        got: list = []
        watchers = [r.watch(c["id"], got if c["id"] == victim["id"] else []) for c in held]
        # A watcher thread that is still alive has not had its GET answered, which is the
        # only thing a client can observe about being parked -- and is exactly what the
        # server reads as "someone is listening".
        time.sleep(1.5)
        r.check("every held booking has a parked watcher",
                all(w.is_alive() for w in watchers),
                ", ".join(str(c["id"]) for c in held))
        began = time.monotonic()
        important = r.book(priority="important", gpu=victim["gpu"],
                           vram_gib=max(free // MIB_PER_GIB, args.gib), duration_minutes=5)
        r.check("an important booking succeeds against a lendable one",
                important is not None,
                "" if important is None else
                f"revoked {[x['id'] for x in important['revoked']]}")
        for w in watchers:
            w.join(30)
        waited = time.monotonic() - began
        if r.check("the watcher was woken, not polled", bool(got) and got[0][0] == 200,
                   f"{waited:.2f}s"):
            ev = got[0][1]
            r.check("the event says the card was taken and by whom",
                    ev.get("event") == "taken" and bool(ev.get("by")), json.dumps(ev))
            r.check("the notification arrived in under two seconds", waited < 2.0,
                    f"{waited:.2f}s")

        # 5. the revoked booking was truncated, not cancelled
        status, out = call("GET", f"{r.base}/api/claims")
        rows = {c["id"]: c for c in (out or {}).get("claims", [])}
        got_victim = rows.get(victim["id"], {})
        r.check("the revoked booking is truncated, not cancelled",
                got_victim.get("cancelled_at") is None
                and got_victim.get("end", "") < victim["end"],
                f"{victim['end']} -> {got_victim.get('end')}")
    finally:
        r.cleanup()

    print(f"\n{'all steps passed' if r.failures == 0 else str(r.failures) + ' step(s) failed'}")
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
