"""CPU and RAM of the box, per user. Display only: nothing here is booked or alerted on.

The parsers take /proc text and the usage computation takes two samples, so all of this
is pure and testable without /proc; probe.probe_host is what reads the files.

CPU is an average over the interval between two samples, from cumulative CPU time
(utime + stime per process, the 'cpu' line of /proc/stat for the box). Per-user RAM is
the sum of each process's resident set, so pages shared between processes count more
than once. Users below UID 1000 are reported as "system" by the probe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MIB_PER_GIB = 1024
FLOOR_CORES = 0.5          # users below both floors are not listed or stored
FLOOR_RSS_MIB = 1024
UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class ProcTicks:
    pid: int
    start: int                 # start time in clock ticks since boot (tells reused pids apart)
    user: str | None           # None: owner could not be resolved
    ticks: int                 # utime + stime, cumulative
    rss_kib: int


@dataclass(frozen=True)
class HostSample:
    taken_at: datetime
    uptime_s: float
    ncpu: int
    cpu_busy: int              # cumulative ticks, all CPUs
    cpu_total: int
    load1: float
    mem_total_kib: int
    mem_avail_kib: int
    swap_total_kib: int
    swap_free_kib: int
    procs: tuple[ProcTicks, ...]


@dataclass(frozen=True)
class UserUsage:
    user: str
    cores: float | None        # None on the first sample: there is no interval yet
    rss_mib: int


@dataclass(frozen=True)
class HostUsage:
    ncpu: int
    cores_busy: float | None
    load1: float
    mem_total_mib: int
    mem_used_mib: int
    swap_total_mib: int
    swap_used_mib: int
    users: tuple[UserUsage, ...]


def parse_stat_cpu(text: str) -> tuple[int, int]:
    """(busy, total) ticks from the aggregate 'cpu' line. guest and guest_nice are already
    included in user and nice, so only the first eight fields are summed."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:9]]
            total = sum(f)
            return total - f[3] - f[4], total          # idle and iowait are not busy
    raise ValueError("no cpu line")


def parse_meminfo(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0])
    return out


def parse_loadavg(text: str) -> float:
    return float(text.split()[0])


def parse_pid_stat(text: str) -> tuple[int, int, int]:
    """(starttime, utime + stime, rss pages) from /proc/<pid>/stat. The command name is
    in parentheses and may itself contain spaces and ')', so split at the last ')'."""
    rest = text[text.rindex(")") + 2:].split()
    # rest[0] is field 3 (state), so field n is rest[n - 3].
    return int(rest[19]), int(rest[11]) + int(rest[12]), int(rest[21])


def usage(prev: HostSample | None, cur: HostSample, clk_tck: int) -> HostUsage:
    dt = (cur.taken_at - prev.taken_at).total_seconds() if prev is not None else 0
    cores_busy = None
    if prev is not None and dt > 0 and cur.cpu_total > prev.cpu_total:
        cores_busy = (cur.cpu_busy - prev.cpu_busy) / (cur.cpu_total - prev.cpu_total) * cur.ncpu
    before = {p.pid: p for p in prev.procs} if prev is not None else {}
    ticks: dict[str, int] = {}
    rss: dict[str, int] = {}
    for p in cur.procs:
        user = p.user or UNATTRIBUTED
        rss[user] = rss.get(user, 0) + p.rss_kib
        old = before.get(p.pid)
        if old is not None and old.start == p.start:
            spent = max(p.ticks - old.ticks, 0)
        elif prev is not None and p.start / clk_tck >= prev.uptime_s:
            spent = p.ticks                    # started during the interval
        else:
            spent = 0                          # unseen last time but older: no baseline
        ticks[user] = ticks.get(user, 0) + spent
    users = []
    for user in rss:
        cores = ticks[user] / clk_tck / dt if dt > 0 else None
        rss_mib = rss[user] // 1024
        if (cores or 0) >= FLOOR_CORES or rss_mib >= FLOOR_RSS_MIB:
            users.append(UserUsage(user, cores, rss_mib))
    users.sort(key=lambda u: (-(u.cores or 0), -u.rss_mib, u.user))
    return HostUsage(
        ncpu=cur.ncpu, cores_busy=cores_busy, load1=cur.load1,
        mem_total_mib=cur.mem_total_kib // 1024,
        mem_used_mib=(cur.mem_total_kib - cur.mem_avail_kib) // 1024,
        swap_total_mib=cur.swap_total_kib // 1024,
        swap_used_mib=(cur.swap_total_kib - cur.swap_free_kib) // 1024,
        users=tuple(users))


class HostTracker:
    """Keeps the previous sample so each poll reports CPU over the interval since the last.
    After a gap longer than max_gap_s (the monitor was down) the average starts over."""

    def __init__(self, clk_tck: int = 100, max_gap_s: float = 300):
        self.clk_tck = clk_tck
        self.max_gap_s = max_gap_s
        self._prev: HostSample | None = None

    def observe(self, sample: HostSample) -> HostUsage:
        prev = self._prev
        if prev is not None and (sample.taken_at - prev.taken_at).total_seconds() > self.max_gap_s:
            prev = None
        self._prev = sample
        return usage(prev, sample, self.clk_tck)
