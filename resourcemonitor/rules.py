"""Rules are pure functions of a Snapshot. No I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass

from resourcemonitor.claims import Booking
from resourcemonitor.model import Snapshot
from resourcemonitor.policy import Policy


@dataclass(frozen=True)
class Alert:
    kind: str                 # "over_booking" | "booked_gpu" | "idle" | "capacity" | "unattributed" | "report"
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
    GPU are one incident, so their VRAM is summed.
    """
    out: list[Alert] = []
    total = {g.index: g.total_mib for g in snap.gpus}
    for gpu, active in _active(bookings, snap).items():
        booked_by_user: dict[str, int] = {}
        for b in active:
            booked_by_user[b.user] = booked_by_user.get(b.user, 0) + b.vram_mib
        booked = sum(booked_by_user.values())
        use: dict[str, int] = {}               # insertion order = first process seen
        for p in snap.procs:
            if p.gpu_index == gpu and p.user is not None:   # unattributed: never accuse
                use[p.user] = use.get(p.user, 0) + p.used_mib
        for user, mib in use.items():
            if user in booked_by_user and mib > 1.10 * booked_by_user[user]:
                out.append(Alert(
                    kind="over_booking", key=f"booking:over:{user}:{gpu}",
                    gpu_index=gpu, user=user,
                    text=(f"{user} uses {_mib(mib)} on GPU {gpu} "
                          f"but booked {_mib(booked_by_user[user])}.")))
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
