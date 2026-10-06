"""Rules are pure functions of a Snapshot. No I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass

from resourcemonitor.model import Snapshot
from resourcemonitor.policy import Policy


@dataclass(frozen=True)
class Alert:
    kind: str                 # "allocation" | "idle" | "capacity" | "unattributed" | "report"
    key: str                  # incident identity; stable across polls
    text: str
    gpu_index: int | None = None
    user: str | None = None


def _mib(n: int) -> str:
    return f"{n / 1024:.1f} GiB"


def check_allocation(snap: Snapshot, pol: Policy) -> list[Alert]:
    """A process on a GPU assigned to somebody else."""
    out = []
    for p in snap.procs:
        if p.user is None:
            continue                       # unattributed: never accuse
        assignee = pol.owner_of_gpu(p.gpu_index)
        if assignee is None or assignee == p.user:
            continue
        out.append(Alert(
            kind="allocation",
            key=f"allocation:{p.user}:{p.gpu_index}",   # not the PID: one incident
            gpu_index=p.gpu_index,
            user=p.user,
            text=(f"{p.user} is using GPU {p.gpu_index} ({_mib(p.used_mib)}), "
                  f"which is assigned to {assignee}."),
        ))
    return out


def check_unattributed(snap: Snapshot, pol: Policy) -> list[Alert]:
    """A holder whose owner could not be resolved, on a GPU that has an assignee.

    Says so rather than staying silent or guessing; deliberately non-accusing.
    """
    out = []
    for p in snap.procs:
        if p.user is not None or p.used_mib < pol.idle_min_mib:
            continue
        assignee = pol.owner_of_gpu(p.gpu_index)
        if assignee is None:
            continue
        out.append(Alert(
            kind="unattributed",
            key=f"unattributed:{p.gpu_index}",
            gpu_index=p.gpu_index,
            user=None,
            text=(f"An unattributed process is holding {_mib(p.used_mib)} on GPU "
                  f"{p.gpu_index} (assigned to {assignee}); its owner could not be "
                  f"resolved."),
        ))
    return out


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
