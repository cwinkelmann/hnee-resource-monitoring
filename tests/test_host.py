"""CPU and RAM: /proc parsers and the pure usage computation (no /proc needed)."""
from datetime import datetime, timedelta, timezone

import pytest

from resourcemonitor.host import (HostSample, HostTracker, ProcTicks, parse_loadavg,
                                  parse_meminfo, parse_pid_stat, parse_stat_cpu, usage)

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
GIB_KIB = 1024 * 1024
TCK = 100


def test_parse_stat_cpu_counts_iowait_as_idle_and_skips_guest():
    text = "cpu  100 5 50 800 40 3 2 0 7 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
    assert parse_stat_cpu(text) == (160, 1000)       # busy = total - idle - iowait


def test_parse_meminfo_reads_kib():
    text = "MemTotal:  2000 kB\nMemFree: 100 kB\nMemAvailable:  1500 kB\nSwapTotal: 0 kB\n"
    m = parse_meminfo(text)
    assert (m["MemTotal"], m["MemAvailable"], m["SwapTotal"]) == (2000, 1500, 0)


def test_parse_loadavg():
    assert parse_loadavg("12.50 10.00 8.00 3/3000 12345\n") == 12.5


def test_parse_pid_stat_survives_spaces_and_parens_in_the_name():
    rest = ["S"] + ["0"] * 10 + ["300", "200"] + ["0"] * 6 + ["4242", "0", "1000"] + ["0"] * 20
    text = "1234 (evil ) name (x)) " + " ".join(rest)
    assert parse_pid_stat(text) == (4242, 500, 1000)   # start, utime+stime, rss pages


def _sample(t, busy, total, procs, uptime=1000.0, avail=1500 * GIB_KIB):
    return HostSample(taken_at=t, uptime_s=uptime, ncpu=8, cpu_busy=busy, cpu_total=total,
                      load1=2.0, mem_total_kib=2000 * GIB_KIB, mem_avail_kib=avail,
                      swap_total_kib=4 * GIB_KIB, swap_free_kib=3 * GIB_KIB, procs=tuple(procs))


def _p(pid, user, ticks, rss_gib, start=10):
    return ProcTicks(pid=pid, start=start, user=user, ticks=ticks, rss_kib=int(rss_gib * GIB_KIB))


def test_first_sample_has_ram_but_no_cpu():
    u = usage(None, _sample(T0, 0, 0, [_p(1, "alice", 50, 2)]), TCK)
    assert u.cores_busy is None
    assert (u.mem_total_mib, u.mem_used_mib, u.swap_total_mib, u.swap_used_mib) == (
        2000 * 1024, 500 * 1024, 4096, 1024)
    assert [(x.user, x.cores, x.rss_mib) for x in u.users] == [("alice", None, 2048)]


def test_cores_are_the_average_over_the_interval():
    prev = _sample(T0, 1000, 8000, [_p(1, "alice", 0, 2), _p(2, "alice", 100, 1),
                                    _p(3, "bob", 0, 0.1)])
    cur = _sample(T0 + timedelta(seconds=60), 1000 + 2400, 8000 + 4800,
                  [_p(1, "alice", 12000, 2), _p(2, "alice", 100 + 6000, 1),
                   _p(3, "bob", 30, 0.1)], uptime=1060.0)
    u = usage(prev, cur, TCK)
    assert u.cores_busy == pytest.approx(4.0)                 # 2400/4800 of 8 cpus
    users = {x.user: x for x in u.users}
    assert users["alice"].cores == pytest.approx(3.0)         # (12000 + 6000) / 100 / 60
    assert users["alice"].rss_mib == 3072
    assert "bob" not in users                                 # 0.005 cores, 0.1 GiB: below floor


def test_exited_restarted_and_new_processes():
    prev = _sample(T0, 0, 100, [_p(1, "alice", 5000, 1), _p(2, "bob", 9000, 1, start=10)],
                   uptime=1000.0)
    cur = _sample(T0 + timedelta(seconds=10), 0, 200,
                  [_p(2, "bob", 600, 1, start=100100),         # pid reused: started after prev
                   _p(3, "carol", 700, 1, start=100500),       # new since prev (1005 s)
                   _p(4, "dave", 99999, 1, start=50000)],      # older than prev but unseen
                  uptime=1010.0)
    users = {x.user: x.cores for x in usage(prev, cur, TCK).users}
    assert users == {"bob": pytest.approx(0.6), "carol": pytest.approx(0.7), "dave": 0.0}


def test_users_sorted_by_cores_then_ram():
    prev = _sample(T0, 0, 0, [_p(1, "a", 0, 1), _p(2, "b", 0, 5), _p(3, "c", 0, 9)])
    cur = _sample(T0 + timedelta(seconds=10), 0, 0,
                  [_p(1, "a", 1000, 1), _p(2, "b", 0, 5), _p(3, "c", 0, 9)], uptime=1010.0)
    assert [x.user for x in usage(prev, cur, TCK).users] == ["a", "c", "b"]


def test_unresolved_owner_is_reported_as_unattributed():
    u = usage(None, _sample(T0, 0, 0, [_p(1, None, 0, 4)]), TCK)
    assert [x.user for x in u.users] == ["unattributed"]


def test_tracker_remembers_the_previous_sample():
    tr = HostTracker(clk_tck=TCK)
    assert tr.observe(_sample(T0, 0, 800, [])).cores_busy is None
    assert tr.observe(_sample(T0 + timedelta(seconds=60), 400, 1600, [],
                              uptime=1060.0)).cores_busy == pytest.approx(4.0)


def test_a_long_gap_restarts_the_average():
    tr = HostTracker(clk_tck=TCK, max_gap_s=300)
    tr.observe(_sample(T0, 0, 800, []))
    assert tr.observe(_sample(T0 + timedelta(minutes=10), 400, 1600, [],
                              uptime=1600.0)).cores_busy is None
