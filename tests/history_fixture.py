"""Deterministic synthetic history shared by the history, query and web tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from resourcemonitor.energy import EnergyLedger
from resourcemonitor.history import HistoryWriter
from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.rules import Alert

TOTAL_MIB = 81559
IDLE_W = 66.0
DORIAN = "dorian.zwanzig"


def _snapshot(t: datetime, elapsed_s: int) -> Snapshot:
    gpus, procs = [], []
    for i in range(8):
        used, util, power = 4, 0, IDLE_W
        if i in (6, 7):
            used, util, power = 22715, 100, 575.0
            pid = 1000 + elapsed_s // (6 * 3600) if i == 6 else 2000
            procs.append(GpuProcess(pid, i, used, DORIAN, "python kev_run.py"))
        elif i == 5 and t.hour % 2 == 0:
            used, util, power = 12000, 60, 300.0
            procs.append(GpuProcess(3000, 5, used, "cwinkelmann", "python train.py"))
        elif i == 4:
            used, util, power = 2048, 0, 70.0
            procs.append(GpuProcess(4000, 4, used, None, None))
        gpus.append(GpuState(i, TOTAL_MIB, used, util, power))
    return Snapshot(t, tuple(gpus), tuple(procs))


def build_history(path: Path | str, start: datetime, hours: int, interval_s: int = 300,
                  gap: tuple[datetime, datetime] | None = None) -> None:
    ledger = EnergyLedger(max_gap_s=interval_s * 5)
    writer = HistoryWriter(path)
    try:
        first = True
        for k in range(hours * 3600 // interval_s):
            t = start + timedelta(seconds=k * interval_s)
            if gap is not None and gap[0] <= t < gap[1]:
                continue
            snap = _snapshot(t, k * interval_s)
            dt_s, rows = ledger.accumulate(snap)
            alert = Alert(kind="allocation", key=f"allocation:{DORIAN}:6",
                          text=f"{DORIAN} holds GPU 6", gpu_index=6, user=DORIAN)
            writer.record(snap, dt_s, rows, [alert], {alert.key} if first else set())
            first = False
    finally:
        writer.close()
