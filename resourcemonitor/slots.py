"""When does a booking shape fit, and in what order may pending bookings move?

Pure arithmetic over Booking rows: no SQL, no clock of its own, no I/O. That is the
same seam probe.py sits behind -- it is why every rule in this file is testable on a
laptop with no GPU.

Booked VRAM on a card only *rises* at a booking's start and only *falls* at its end, so
the earliest instant a new window can begin is `now` or some booking's end. That finite
candidate set is the whole trick; there is no search and no loop over time."""
from __future__ import annotations

from datetime import datetime, timedelta

from resourcemonitor.claims import Booking

HORIZON = timedelta(days=14)        # matches claims.MAX_DAYS: never offer a slot past it


def _live(bookings: list[Booking], gpu: int) -> list[Booking]:
    return [b for b in bookings if b.gpu == gpu and b.cancelled_at is None]


def booked_at(bookings: list[Booking], gpu: int, t: datetime) -> int:
    """MiB booked on `gpu` at instant t, cancelled rows excluded."""
    return sum(b.vram_mib for b in _live(bookings, gpu) if b.start <= t < b.end)


def fits(bookings: list[Booking], gpu: int, vram_mib: int, start: datetime,
         duration: timedelta, card_mib: int) -> bool:
    """Does `vram_mib` fit on `gpu` for the whole of [start, start + duration)?
    Checking `start` and every booking start strictly inside the window covers every
    instant, because booked VRAM only rises at a start."""
    if vram_mib > card_mib:
        return False
    end = start + duration
    rows = _live(bookings, gpu)
    instants = [start] + sorted(b.start for b in rows if start < b.start < end)
    return all(booked_at(rows, gpu, t) + vram_mib <= card_mib for t in instants)


def earliest_fit(bookings: list[Booking], gpu: int, vram_mib: int, duration: timedelta,
                 card_mib: int, now: datetime) -> datetime | None:
    """The first instant at or after `now` where this shape fits on this card for its
    whole duration, or None if that is beyond HORIZON (or never)."""
    rows = _live(bookings, gpu)
    limit = now + HORIZON
    candidates = [now] + sorted({b.end for b in rows if now < b.end <= limit})
    for t in candidates:
        if fits(rows, gpu, vram_mib, t, duration, card_mib):
            return t
    return None


def earliest_fit_group(bookings: list[Booking], count: int, vram_mib: int,
                       duration: timedelta, cards: dict[int, int], now: datetime,
                       gpus: list[int] | None = None) -> tuple[datetime, list[int]] | None:
    """The earliest instant where `count` cards can each host `vram_mib` for the whole
    duration, with the lowest-index fitting cards chosen. All members share one start --
    a group assigned staggered starts would sit holding idle cards, which is the whole
    reason groups exist. count=1 is also how a booking with no chosen card picks one."""
    allowed = sorted(cards if gpus is None else [g for g in gpus if g in cards])
    if count < 1 or count > len(allowed):
        return None
    limit = now + HORIZON
    candidates = [now] + sorted({b.end for b in bookings
                                 if b.cancelled_at is None and now < b.end <= limit
                                 and b.gpu in cards})
    for t in candidates:
        free = [g for g in allowed
                if fits(bookings, g, vram_mib, t, duration, cards[g])]
        if len(free) >= count:
            return t, free[:count]
    return None


def advance_order(bookings: list[Booking]) -> list[Booking]:
    """The order in which pending bookings get offered freed capacity: important first,
    then the earliest start, then whoever asked first. One group member stands for its
    group, which is why the id is only the final tie-break."""
    return sorted(bookings, key=lambda b: (b.priority != "important", b.start,
                                           b.created_at, b.id))
