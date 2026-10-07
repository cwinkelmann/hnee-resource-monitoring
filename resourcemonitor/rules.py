"""Rules are pure functions of a Snapshot. No I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from resourcemonitor.claims import Booking, allocate, takes_of
from resourcemonitor.model import Snapshot
from resourcemonitor.policy import Policy


@dataclass(frozen=True)
class Alert:
    kind: str                 # "over_booking" | "booked_gpu" | "taken" | "idle" | "capacity" | "unattributed" | "report"
    key: str                  # incident identity; stable across polls
    text: str
    gpu_index: int | None = None
    user: str | None = None


def _mib(n: int) -> str:
    return f"{n / 1024:.1f} GiB"


def _active(bookings: list[Booking], snap: Snapshot) -> dict[int, list[Booking]]:
    by_gpu: dict[int, list[Booking]] = {}
    for b in bookings:
        if b.active_at(snap.taken_at):
            by_gpu.setdefault(b.gpu, []).append(b)
    return by_gpu


def check_bookings(snap: Snapshot, pol: Policy, bookings: list[Booking]) -> list[Alert]:
    """Over-use of a booked share, and non-bookers crowding a booked card.

    A GPU without an active booking is free. Several processes of one user on one
    GPU are one incident, so their VRAM is summed. Shares are effective ones: a lendable
    booking squeezed by an important booking counts only what allocate() leaves it.
    """
    out: list[Alert] = []
    total = {g.index: g.total_mib for g in snap.gpus}
    for gpu, active in _active(bookings, snap).items():
        eff = allocate(active, total[gpu]) if gpu in total else {b.id: b.vram_mib for b in active}
        booked_by_user: dict[str, int] = {}
        share_by_user: dict[str, int] = {}
        for b in active:
            booked_by_user[b.user] = booked_by_user.get(b.user, 0) + b.vram_mib
            share_by_user[b.user] = share_by_user.get(b.user, 0) + eff[b.id]
        booked = sum(share_by_user.values())
        use: dict[str, int] = {}               # insertion order = first process seen
        for p in snap.procs:
            if p.gpu_index == gpu and p.user is not None:   # unattributed: never accuse
                use[p.user] = use.get(p.user, 0) + p.used_mib
        for user, mib in use.items():
            if user in share_by_user and mib * 10 > share_by_user[user] * 11:
                share, asked = share_by_user[user], booked_by_user[user]
                if share < asked:
                    takers = sorted({b.user for b in active if b.priority == "important"})
                    text = (f"{user} uses {_mib(mib)} on GPU {gpu}; {user}'s lendable booking "
                            f"was reduced from {_mib(asked)} to {_mib(share)} by "
                            f"{', '.join(takers)}'s important booking.")
                else:
                    text = f"{user} uses {_mib(mib)} on GPU {gpu} but booked {_mib(asked)}."
                out.append(Alert(kind="over_booking", key=f"booking:over:{user}:{gpu}",
                                 gpu_index=gpu, user=user, text=text))
        others = {u: m for u, m in use.items() if u not in booked_by_user}
        remainder = max(total.get(gpu, 0) - booked, 0)
        if sum(others.values()) > remainder:
            earliest_end = min(b.end for b in active)
            for user, mib in others.items():
                out.append(Alert(
                    kind="booked_gpu", key=f"booking:other:{user}:{gpu}",
                    gpu_index=gpu, user=user,
                    text=(f"{user} is using {_mib(mib)} on GPU {gpu}; {_mib(booked)} is "
                          f"booked by {', '.join(sorted(booked_by_user))} until "
                          f"{earliest_end:%a %H:%M} UTC, {_mib(remainder)} unbooked.")))
    return out


def check_takes(now: datetime, bookings: list[Booking], totals: dict[int, int]) -> list[Alert]:
    """A heads-up to the holder of a lendable booking that an important booking takes part of
    it, from the moment the important booking exists (so during its grace period) until the
    take ends. One alert per (important, lendable) pair; the key is made of two row ids, so
    the cooldown store deduplicates it. `bookings` are uncancelled rows from now onwards."""
    out: list[Alert] = []
    live = [b for b in bookings if b.cancelled_at is None and b.end > now]
    for imp in sorted((b for b in live if b.priority == "important"), key=lambda b: b.id):
        if imp.gpu not in totals:
            continue
        per: dict[int, list[dict]] = {}
        for t in takes_of(imp, live, totals[imp.gpu]):
            if t["end"] > now:
                per.setdefault(t["id"], []).append(t)
        for lend_id, segs in per.items():
            first, last = segs[0], segs[-1]
            out.append(Alert(
                kind="taken", key=f"booking:taken:{imp.id}:{lend_id}",
                gpu_index=imp.gpu, user=first["user"],
                text=(f"{imp.user} takes {_mib(max(s['vram_mib'] for s in segs))} of "
                      f"{first['user']}'s lendable booking on GPU {imp.gpu} from "
                      f"{max(first['start'], now):%a %H:%M} until {last['end']:%a %H:%M} UTC.")))
    return out


def check_unattributed(snap: Snapshot, pol: Policy, bookings: list[Booking]) -> list[Alert]:
    """Holders whose owner could not be resolved, on a GPU that has an active booking.

    One alert per GPU, summing the processes that each hold at least idle_min_mib.
    Says so rather than staying silent or guessing; deliberately non-accusing.
    """
    active = _active(bookings, snap)
    held: dict[int, int] = {}
    for p in snap.procs:
        if p.user is not None or p.used_mib < pol.idle_min_mib:
            continue
        if p.gpu_index not in active:
            continue
        held[p.gpu_index] = held.get(p.gpu_index, 0) + p.used_mib
    return [Alert(
        kind="unattributed",
        key=f"unattributed:{gpu}",
        gpu_index=gpu,
        user=None,
        text=(f"An unattributed process is holding {_mib(mib)} on GPU "
              f"{gpu} (booked by {', '.join(sorted({b.user for b in active[gpu]}))}); "
              f"its owner could not be resolved."),
    ) for gpu, mib in held.items()]


def check_capacity(snap: Snapshot, pol: Policy) -> list[Alert]:
    """Nobody can start a job: no GPU has capacity_free_mib free."""
    free = {g.index: g.total_mib - g.used_mib for g in snap.gpus}
    if any(f >= pol.capacity_free_mib for f in free.values()):
        return []
    best = max(free, key=free.get) if free else None
    return [Alert(
        kind="capacity",
        key="capacity:all",
        text=(f"No GPU has {_mib(pol.capacity_free_mib)} free — the box is full. "
              f"Most free: GPU {best} with {_mib(free[best])}." if best is not None
              else "No GPUs reported."),
    )]


class IdleTracker:
    """Remembers when each process last looked busy.

    GPU utilisation is per-DEVICE, not per-process, so "idle" here means the card
    this process sits on is below the threshold. With one process per card that is
    exact; with two it is conservative, which is the right direction -- it under-reports
    rather than accusing a busy job of being parked.
    """

    def __init__(self) -> None:
        self._busy_since: dict[int, object] = {}   # pid -> last time it looked busy

    def tracked(self) -> int:
        return len(self._busy_since)

    def observe(self, snap: Snapshot, pol: Policy) -> list[Alert]:
        util = {g.index: g.util_pct for g in snap.gpus}
        live = {p.pid for p in snap.procs}
        for pid in list(self._busy_since):
            if pid not in live:
                del self._busy_since[pid]          # exited; forget it

        out = []
        for p in snap.procs:
            if p.used_mib < pol.idle_min_mib:
                self._busy_since.pop(p.pid, None)
                continue
            if util.get(p.gpu_index, 100) > pol.idle_util_pct:
                self._busy_since[p.pid] = snap.taken_at
                continue
            first = self._busy_since.setdefault(p.pid, snap.taken_at)
            idle_s = (snap.taken_at - first).total_seconds()
            if idle_s >= pol.idle_grace_s:
                out.append(Alert(
                    kind="idle",
                    key=f"idle:{p.user}:{p.gpu_index}:{p.pid}",
                    gpu_index=p.gpu_index,
                    user=p.user,
                    text=(f"{p.user or 'an unattributed process'} has held "
                          f"{_mib(p.used_mib)} on GPU {p.gpu_index} at "
                          f"{util.get(p.gpu_index, 0)}% utilisation for "
                          f"{int(idle_s // 60)} minutes."),
                ))
        return out
