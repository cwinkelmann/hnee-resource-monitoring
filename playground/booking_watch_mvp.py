"""Spike: the parent listens to the booking server's state instead of a file.

Books a GPU slot on the server, runs the job as a child process, and polls
GET /api/claims. While the booking stands (present, not cancelled, not past
its end) the child keeps running. A cancellation on the server is the
revocation notice: the parent stops its own child. A job that finishes on
its own cancels the booking, so the card frees early.

Stdlib only; the parent only ever stops its *own* child.

Try it (against t14):
    python playground/booking_watch_mvp.py --server http://192.168.188.88:8765 \
        --user christian --minutes 10 --seconds 300
    # then cancel booking <id> in the dashboard, or:
    #   POST /api/claims/<id>/cancel
"""

import argparse
import json
import multiprocessing
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _sleep_worker(stop: multiprocessing.Event, work_seconds: float) -> None:
    """The 'job': sleep for work_seconds, waking often enough to see a stop."""
    deadline = time.monotonic() + work_seconds
    while time.monotonic() < deadline:
        if stop.wait(timeout=0.05):
            return


def _api(server: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        server + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={} if body is None else {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def book(server: str, user: str, minutes: int, vram_gib: int | None) -> dict:
    body = {"user": user, "duration_minutes": minutes, "priority": "lendable",
            "note": "booking_watch_mvp"}
    if vram_gib is not None:
        body["vram_gib"] = vram_gib
    return _api(server, "/api/claims", body)["claim"]


def booking_state(server: str, claim_id: int) -> str:
    """'standing', 'revoked' (cancelled or gone), 'taken' (an important booking
    holds this booking's VRAM right now) or 'expired' (past its end)."""
    claims = _api(server, "/api/claims")["claims"]
    mine = next((c for c in claims if c["id"] == claim_id), None)
    if mine is None or mine["cancelled_at"] is not None:
        return "revoked"
    now = datetime.now(timezone.utc)
    if datetime.fromisoformat(mine["end"]) <= now:
        return "expired"
    for seg in mine.get("taken", ()):
        if datetime.fromisoformat(seg["start"]) <= now < datetime.fromisoformat(seg["end"]):
            print(f"{seg['vram_mib']} MiB taken by {', '.join(seg['by'])} "
                  f"until {seg['end']}", flush=True)
            return "taken"
    return "standing"


def run_job(server: str, claim_id: int, work_seconds: float,
            poll_seconds: float = 2.0) -> str:
    stop = multiprocessing.Event()
    child = multiprocessing.Process(
        target=_sleep_worker, args=(stop, work_seconds), daemon=True
    )
    child.start()
    try:
        while child.is_alive():
            try:
                state = booking_state(server, claim_id)
            except (urllib.error.URLError, OSError) as e:
                print(f"server unreachable, job continues: {e}", flush=True)
                time.sleep(poll_seconds)
                continue
            if state != "standing":
                stop.set()  # graceful: the worker exits its sleep loop
                child.join(timeout=5.0)
                if child.is_alive():
                    child.terminate()  # our own child only
                    child.join()
                return state
            time.sleep(poll_seconds)
        return "completed"
    finally:
        if child.is_alive():
            stop.set()
            child.join()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default="http://192.168.188.88:8765",
                        help="booking server (default: t14)")
    parser.add_argument("--user", required=True, help="who the booking belongs to")
    parser.add_argument("--minutes", type=int, default=10, help="booking length")
    parser.add_argument("--vram", type=int, default=None,
                        help="GiB to book (default: the whole card)")
    parser.add_argument("--seconds", type=float, default=60.0, help="length of the fake job")
    parser.add_argument("--poll", type=float, default=2.0, help="poll interval in seconds")
    args = parser.parse_args(argv)

    claim = book(args.server, args.user, args.minutes, args.vram)
    print(f"booked: id={claim['id']} gpu={claim['gpu']} "
          f"{claim['start']} .. {claim['end']}", flush=True)

    outcome = run_job(args.server, claim["id"], args.seconds, args.poll)
    print(outcome)

    if outcome == "completed":  # free the card early; best effort
        try:
            _api(args.server, f"/api/claims/{claim['id']}/cancel", body={})
        except (urllib.error.URLError, OSError) as e:
            print(f"early release failed: {e}", flush=True)

    try:  # the booking's final state, whoever changed it last
        claims = _api(args.server, "/api/claims")["claims"]
        mine = next((c for c in claims if c["id"] == claim["id"]), None)
        print("final booking state:", json.dumps(mine, indent=2))
    except (urllib.error.URLError, OSError):
        pass
    return 0 if outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
