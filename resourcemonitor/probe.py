"""The only module that shells out. Everything else is pure."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot

GPU_FIELDS = "index,uuid,memory.total,memory.used,utilization.gpu,power.draw"
APP_FIELDS = "gpu_uuid,pid,used_memory"


def _num(cell: str) -> int:
    """'22715 MiB' -> 22715, '98 %' -> 98. nvidia-smi units break a bare int()."""
    return int(cell.strip().split()[0])


def _watts(cell: str) -> float:
    """'575.00 W' -> 575.0, '[N/A]' -> 0.0.

    Kept as a float: truncating 63.80 W loses 1.3% of an idle card, and the energy
    figures are a running sum, so that error compounds over a week.
    """
    try:
        return float(cell.strip().split()[0])
    except ValueError:
        return 0.0          # card does not report power; not a reason to fail the poll


def parse_gpu_query(text: str) -> tuple[GpuState, ...]:
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        index, _uuid, total, used, util, power = [c.strip() for c in line.split(",")]
        out.append(GpuState(int(index), _num(total), _num(used), _num(util), _watts(power)))
    return tuple(out)


def parse_gpu_uuids(text: str) -> dict[str, int]:
    out = {}
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        index, uuid, *_ = [c.strip() for c in line.split(",")]
        out[uuid] = int(index)
    return out


def parse_apps_query(text: str, uuid_to_index: dict[str, int]) -> tuple[GpuProcess, ...]:
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        uuid, pid, used = [c.strip() for c in line.split(",")]
        if uuid not in uuid_to_index:
            continue          # a GPU we did not enumerate; drop rather than guess
        out.append(GpuProcess(int(pid), uuid_to_index[uuid], _num(used)))
    return tuple(out)


def _run(fields: str, query: str) -> str:
    return subprocess.run(
        ["nvidia-smi", f"--query-{query}={fields}", "--format=csv,noheader"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


def probe() -> Snapshot:
    gpu_text = _run(GPU_FIELDS, "gpu")
    apps_text = _run(APP_FIELDS, "compute-apps")
    return Snapshot(
        taken_at=datetime.now(timezone.utc),
        gpus=parse_gpu_query(gpu_text),
        procs=parse_apps_query(apps_text, parse_gpu_uuids(gpu_text)),
    )
