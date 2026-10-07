"""The only module that shells out. Everything else is pure."""
from __future__ import annotations

import os
import posixpath
import pwd
import re
import subprocess
from datetime import datetime, timezone

from resourcemonitor.host import (HostSample, ProcTicks, parse_loadavg, parse_meminfo,
                                  parse_pid_stat, parse_stat_cpu)
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
_MODULE = re.compile(r"[A-Za-z_][\w.]*")
_SCRIPT = re.compile(r"[\w.-]+\.py")
_SAFE_TOKEN = re.compile(r"[\w.:/@+-]+")
_NOARG_FLAGS = re.compile(r"-[BdEIOPqsSuvx]+")      # interpreter flags that take no value


def _python_label(args: list[str]) -> str:
    """Walk interpreter options; only a real -m module or the script basename is shown."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-m":
            mod = args[i + 1] if i + 1 < len(args) else ""
            return f"python -m {mod}" if _MODULE.fullmatch(mod) else "python"
        if a in ("-X", "-W"):
            i += 2                      # option with a separate value
        elif _NOARG_FLAGS.fullmatch(a):
            i += 1
        elif a.startswith("-"):
            return "python"             # -c (code), unknown or long options: show nothing
        else:
            base = posixpath.basename(a)
            return f"python {base}" if _SCRIPT.fullmatch(base) else "python"
    return "python"


def _safe_basename(text: str) -> str | None:
    """First whitespace token only (setproctitle can put arguments after it), basename."""
    parts = text.split()
    if not parts or not _SAFE_TOKEN.fullmatch(parts[0]):
        return None
    return posixpath.basename(parts[0]) or None


def label_for(name: str | None, cmdline: list[str] | None) -> str | None:
    """Short, safe label of WHAT a process is.

    The page is unauthenticated on the LAN and arguments can carry tokens, paths or data
    names, so the label may contain only: the interpreter word, `-m <module>`, a script
    basename, or the basename of the nvidia-smi process name. Never any other argument.
    """
    label: str | None = None
    if cmdline and _PYTHON.fullmatch(posixpath.basename(cmdline[0])):
        label = _python_label(cmdline[1:])
    elif name:
        label = _safe_basename(name)
    elif cmdline:
        label = _safe_basename(cmdline[0])
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


SYSTEM_UID_MAX = 999            # below 1000: root and service accounts, reported as "system"
SYSTEM = "system"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def probe_host() -> HostSample:
    """CPU and RAM counters of the box and of every process, from /proc. Processes that
    exit or cannot be read while scanning are skipped, never guessed."""
    taken_at = datetime.now(timezone.utc)
    busy, total = parse_stat_cpu(_read("/proc/stat"))
    mem = parse_meminfo(_read("/proc/meminfo"))
    page_kib = os.sysconf("SC_PAGE_SIZE") // 1024
    names: dict[int, str | None] = {}
    procs = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            start, ticks, rss_pages = parse_pid_stat(_read(f"/proc/{entry}/stat"))
            uid = _parse_status_uid(_read(f"/proc/{entry}/status"))
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            continue
        if uid is None:
            user = None
        elif uid <= SYSTEM_UID_MAX:
            user = SYSTEM
        else:
            if uid not in names:
                names[uid] = _name_of_uid(uid)
            user = names[uid]
        procs.append(ProcTicks(int(entry), start, user, ticks, rss_pages * page_kib))
    return HostSample(
        taken_at=taken_at, uptime_s=float(_read("/proc/uptime").split()[0]),
        ncpu=os.cpu_count() or 1, cpu_busy=busy, cpu_total=total,
        load1=parse_loadavg(_read("/proc/loadavg")),
        mem_total_kib=mem["MemTotal"], mem_avail_kib=mem["MemAvailable"],
        swap_total_kib=mem.get("SwapTotal", 0), swap_free_kib=mem.get("SwapFree", 0),
        procs=tuple(procs))
