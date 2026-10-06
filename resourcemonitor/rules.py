"""Rules are pure functions of a Snapshot. No I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass

from resourcemonitor.model import Snapshot
from resourcemonitor.policy import Policy


@dataclass(frozen=True)
class Alert:
    kind: str                 # "allocation" | "idle" | "capacity"
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
