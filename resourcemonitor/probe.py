"""The only module that shells out. Everything else is pure."""
from __future__ import annotations

import os
import posixpath
import pwd
import re
import subprocess
from datetime import datetime, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot

GPU_FIELDS = "index,uuid,memory.total,memory.used,utilization.gpu,power.draw"
APP_FIELDS = "gpu_uuid,pid,used_memory,process_name"


def _num(cell: str) -> int:
    """'22715 MiB' -> 22715, '98 %' -> 98. nvidia-smi units break a bare int()."""
    try:
        return int(cell.strip().split()[0])
    except (ValueError, IndexError):
        return 0            # '[N/A]' / '[Not Supported]': not a reason to lose the poll


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
        # maxsplit: a process path may itself contain commas. 3-field legacy lines still work.
        cells = [c.strip() for c in line.split(",", 3)]
        uuid, pid, used = cells[:3]
        raw_name = cells[3] if len(cells) > 3 else ""
        name = None if raw_name in ("", "[N/A]") else raw_name
        if uuid not in uuid_to_index:
            continue          # a GPU we did not enumerate; drop rather than guess
        out.append(GpuProcess(int(pid), uuid_to_index[uuid], _num(used), None, name))
    return tuple(out)


def _run(fields: str, query: str) -> str:
    return subprocess.run(
        ["nvidia-smi", f"--query-{query}={fields}", "--format=csv,noheader"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


# Inside a user namespace the kernel rewrites every foreign UID to this value, and pwd
# would resolve it to "nobody" -- a guess, not an attribution.
_OVERFLOW_UID = 65534


def _parse_status_uid(text: str) -> int | None:
    """Real UID from the 'Uid:' line of /proc/<pid>/status, or None if absent/garbled."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def _uid_of(pid: int) -> int | None:
    # Not os.stat(/proc/<pid>): that dir is owned by root for non-dumpable processes,
    # which would name "root" wrongly.
    try:
        with open(f"/proc/{pid}/status") as f:
            return _parse_status_uid(f.read())
    except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeDecodeError):
        return None       # exited between the nvidia-smi call and this read


def _name_of_uid(uid: int) -> str | None:
    if uid == _OVERFLOW_UID:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def owner_of(pid: int) -> str | None:
    """Username that owns `pid`, or None if it cannot be established.

    None is a real answer, not an error: the process may have exited, or this may be
    running somewhere the UID does not resolve. Callers render None as "unattributed"
    and never guess.
    """
    uid = _uid_of(pid)
    if uid is None:
        return None
    return _name_of_uid(uid)


def cmdline_of(pid: int) -> list[str] | None:
    """argv of `pid` from /proc, or None if it cannot be read (exited, no permission)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a] or None


_PYTHON = re.compile(r"python(3(\.\d+)?)?")
_LABEL_MAX = 60


def label_for(name: str | None, cmdline: list[str] | None) -> str | None:
    """Short, safe label of WHAT a process is.

    The page is unauthenticated on the LAN and arguments can carry tokens, paths or data
    names, so the label may contain only: the interpreter word, `-m <module>`, a script
    basename, or the basename of the nvidia-smi process name. Never any other argument.
    """
    label: str | None = None
    if cmdline and _PYTHON.fullmatch(posixpath.basename(cmdline[0])):
        args = cmdline[1:]
        if "-m" in args and args.index("-m") + 1 < len(args):
            label = f"python -m {args[args.index('-m') + 1]}"
        else:
            script = next((a for a in args if a.endswith(".py")), None)
            label = f"python {posixpath.basename(script)}" if script else "python"
    elif name:
        label = posixpath.basename(name) or None
    elif cmdline:
        label = posixpath.basename(cmdline[0]) or None
    return label[:_LABEL_MAX] if label else None


def probe() -> Snapshot:
    gpu_text = _run(GPU_FIELDS, "gpu")
    apps_text = _run(APP_FIELDS, "compute-apps")
    procs = tuple(
        GpuProcess(p.pid, p.gpu_index, p.used_mib, owner_of(p.pid),
                   label_for(p.name, cmdline_of(p.pid)))
        for p in parse_apps_query(apps_text, parse_gpu_uuids(gpu_text))
    )
    return Snapshot(
        taken_at=datetime.now(timezone.utc),
        gpus=parse_gpu_query(gpu_text),
        procs=procs,
    )
