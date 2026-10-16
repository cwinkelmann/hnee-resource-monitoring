"""Who is listening for news about a booking.

A job that holds a card keeps one GET /api/claims/<id>/events parked here, so the server
can tell it in milliseconds that its card has been taken instead of the job polling. The
parked connection is also the only liveness signal there is: when the job dies, TCP drops
and the watch disappears, which is how claims.create() knows whether a declared notice is
worth honouring.

This module knows nothing about HTTP, bookings or SQLite -- only integer ids and dicts.

Lock order, and the whole reason this is a separate module: callers must never hold this
lock while taking the ClaimsStore lock. Events are collected inside the booking
transaction and delivered by web.py *after* COMMIT."""
from __future__ import annotations

import threading


class Watch:
    """One parked connection. Not created directly: see Watchers.register."""

    def __init__(self, registry: "Watchers", claim_id: int):
        self._registry = registry
        self.claim_id = claim_id
        self.event: dict | None = None
        self.closed = False

    def wait(self, timeout: float) -> dict | None:
        """Block until an event arrives, the timeout passes, or the server shuts down."""
        cond = self._registry._cond
        with cond:
            cond.wait_for(lambda: self.event is not None or self.closed
                          or self._registry._down, timeout)
            return self.event

    def close(self) -> None:
        self._registry._drop(self)

    def __enter__(self) -> "Watch":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Watchers:
    def __init__(self, max_watchers: int = 64):
        self.max_watchers = max_watchers
        self._cond = threading.Condition()
        self._by_claim: dict[int, list[Watch]] = {}
        self._down = False

    def register(self, claim_id: int) -> Watch | None:
        """A Watch, or None when the cap is reached (the route then answers 503 and the
        client falls back to polling) or the server is shutting down."""
        with self._cond:
            if self._down or self._count() >= self.max_watchers:
                return None
            watch = Watch(self, claim_id)
            self._by_claim.setdefault(claim_id, []).append(watch)
            return watch

    def attached(self, claim_id: int) -> bool:
        with self._cond:
            return bool(self._by_claim.get(claim_id))

    def notify(self, claim_id: int, event: dict) -> int:
        """Hand `event` to every watcher of this booking and wake them. Returns how many
        there were, which is 0 for a job that is not listening -- not an error."""
        with self._cond:
            watches = list(self._by_claim.get(claim_id, ()))
            for w in watches:
                w.event = event
            if watches:
                self._cond.notify_all()
            return len(watches)

    def count(self) -> int:
        with self._cond:
            return self._count()

    def shutdown(self) -> None:
        """Wake every waiter with nothing, so server_close() is never held up by a parked
        request, and refuse further registrations."""
        with self._cond:
            self._down = True
            self._by_claim.clear()
            self._cond.notify_all()

    def _count(self) -> int:
        return sum(len(v) for v in self._by_claim.values())

    def _drop(self, watch: Watch) -> None:
        with self._cond:
            watch.closed = True
            rest = [w for w in self._by_claim.get(watch.claim_id, ()) if w is not watch]
            if rest:
                self._by_claim[watch.claim_id] = rest
            else:
                self._by_claim.pop(watch.claim_id, None)
            self._cond.notify_all()
