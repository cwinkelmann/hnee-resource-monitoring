"""Immutable view of what the GPUs are doing. No I/O lives here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GpuState:
    index: int
    total_mib: int
    used_mib: int
    util_pct: int
    power_w: float = 0.0      # 0.0 when the card does not report it ('[N/A]')


@dataclass(frozen=True)
class GpuProcess:
    pid: int
    gpu_index: int
    used_mib: int
    user: str | None = None      # None == could not resolve; render as "unattributed"


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    gpus: tuple[GpuState, ...]
    procs: tuple[GpuProcess, ...]

    def free_mib(self, index: int) -> int:
        for g in self.gpus:
            if g.index == index:
                return g.total_mib - g.used_mib
        raise KeyError(f"no GPU with index {index}")
