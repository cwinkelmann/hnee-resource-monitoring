"""Spike: the client side of the revocation watch channel (spec section 6).

A booking's job runs as a child process. The parent polls a watch file:
empty (or absent) means the booking stands and the child keeps running;
any content is the revocation notice and the parent stops the child.

The parent only ever stops its *own* child. Stdlib only.

Try it:
    python -m resourcemonitor.revocation_mvp --watch /tmp/revoke --seconds 60
    # in another shell:  echo revoked > /tmp/revoke
"""

import argparse
import multiprocessing
import os
import sys
import time
from pathlib import Path


def _sleep_worker(stop: multiprocessing.Event, work_seconds: float) -> None:
    """The 'job': sleep for work_seconds, waking often enough to see a stop."""
    deadline = time.monotonic() + work_seconds
    while time.monotonic() < deadline:
        if stop.wait(timeout=0.05):
            return


def _has_content(path: Path) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:  # absent file counts as empty: the booking stands
        return False


def run_job(work_seconds: float, watch_path: Path, poll_seconds: float = 1.0) -> str:
    """Run the sleep job under the watch file. Returns 'completed' or 'revoked'."""
    stop = multiprocessing.Event()
    child = multiprocessing.Process(
        target=_sleep_worker, args=(stop, work_seconds), daemon=True
    )
    child.start()
    try:
        while child.is_alive():
            if _has_content(watch_path):
                stop.set()  # graceful: the worker exits its sleep loop
                child.join(timeout=5.0)
                if child.is_alive():
                    child.terminate()  # our own child only
                    child.join()
                return "revoked"
            time.sleep(poll_seconds)
        return "completed"
    finally:
        if child.is_alive():
            stop.set()
            child.join()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", type=Path, required=True, help="revocation watch file")
    parser.add_argument("--seconds", type=float, default=60.0, help="length of the fake job")
    parser.add_argument("--poll", type=float, default=1.0, help="poll interval in seconds")
    args = parser.parse_args(argv)

    outcome = run_job(args.seconds, args.watch, args.poll)
    print(outcome)
    return 0 if outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
