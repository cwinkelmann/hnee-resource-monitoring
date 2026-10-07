"""CPU and RAM end to end without /proc: history rows, /api/now's box, the timeline runs,
and the watch loop staying up when the CPU/RAM probe fails."""
import sqlite3
from datetime import datetime, timedelta, timezone

from resourcemonitor import cli
from resourcemonitor.history import HistoryWriter
from resourcemonitor.host import HostTracker, HostUsage, UserUsage
from resourcemonitor.queries import latest, open_ro, timeline
from tests.test_history import _snap

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
GIB = 1024


def _box(*users, cores_busy=10.0):
    return HostUsage(ncpu=64, cores_busy=cores_busy, load1=3.5, mem_total_mib=256 * GIB,
                     mem_used_mib=100 * GIB, swap_total_mib=GIB, swap_used_mib=0,
                     users=tuple(UserUsage(u, c, r) for u, c, r in users))


def _write(path, polls):
    """polls: list of (t, dt_s, box or None)."""
    w = HistoryWriter(path)
    for t, dt_s, box in polls:
        w.record(_snap(t), dt_s, [], [], set(), box)
    w.close()


def test_record_and_now(tmp_path):
    db = tmp_path / "h.sqlite"
    _write(db, [(T0, None, _box(("alice", 12.345, 70 * GIB), ("bob", None, 2 * GIB),
                                ("carol", 30.0, 1 * GIB)))])
    conn = open_ro(db)
    box = latest(conn, [], T0, 600)["box"]
    assert box["ncpu"] == 64 and box["cores_busy"] == 10.0 and box["mem_used_mib"] == 100 * GIB
    assert [(u["user"], u["cores"]) for u in box["users"]] == [
        ("carol", 30.0), ("alice", 12.35), ("bob", None)]


def test_now_box_is_none_without_cpu_ram_data(tmp_path):
    db = tmp_path / "h.sqlite"
    _write(db, [(T0, None, None)])
    assert latest(open_ro(db), [], T0, 600)["box"] is None
    conn = sqlite3.connect(db)                  # history from a monitor before CPU/RAM
    conn.executescript("DROP TABLE host_samples; DROP TABLE user_samples;")
    conn.close()
    conn = open_ro(db)
    assert latest(conn, [], T0, 600)["box"] is None
    assert timeline(conn, 24, T0 + M)["box"]["cpu"] == []


def test_timeline_runs_split_at_thresholds_absence_and_gaps(tmp_path):
    db = tmp_path / "h.sqlite"
    hi = 80 * GIB
    _write(db, [
        (T0, None, _box(("alice", 2.0, hi), ("bob", 0.6, 2 * GIB))),
        (T0 + M, 60, _box(("alice", 5.0, hi), ("bob", 1.0, 2 * GIB))),
        (T0 + 2 * M, 60, _box(("alice", 0.5, 70 * GIB))),          # alice below 1 core
        (T0 + 3 * M, 60, _box(("alice", 3.0, hi))),
        (T0 + 10 * M, None, _box(("alice", 3.0, hi))),             # monitoring gap
        (T0 + 11 * M, 60, _box(("alice", None, hi))),              # first poll: no cores
    ])
    box = timeline(open_ro(db), 24, T0 + 12 * M)["box"]
    iso = lambda m: (T0 + m * M).isoformat()
    assert [(r["user"], r["start"], r["end"], r["peak"]) for r in box["cpu"]] == [
        ("alice", iso(0), iso(1), 5.0), ("bob", iso(1), iso(1), 1.0),
        ("alice", iso(3), iso(3), 3.0), ("alice", iso(10), iso(10), 3.0)]
    assert [(r["start"], r["end"], r["peak"], r["ongoing"]) for r in box["ram"]] == [
        (iso(0), iso(3), hi, False),                               # 70 GiB still counts
        (iso(10), iso(11), hi, True)]
    assert (box["cpu_min_cores"], box["ram_min_mib"]) == (1.0, 64 * GIB)


def test_prune_removes_old_cpu_ram_rows(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db, retention_days=1)
    w.record(_snap(T0), None, [], [], set(), _box(("alice", 2.0, 2 * GIB)))
    w.prune(T0 + timedelta(days=2))
    w.close()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_samples").fetchone()[0] == 0


class _Rec:
    def __init__(self):
        self.box = "unset"

    def record(self, snap, dt_s, rows, alerts, sent_keys, box=None):
        self.box = box


def _run(monkeypatch, tmp_path, probe_host):
    from tests.test_cli import _history_setup   # the existing run_once fixtures
    monkeypatch.setattr(cli, "probe_host", probe_host)
    hist = _Rec()
    pol, state, notifier, tracker, ledger, epath = _history_setup(monkeypatch, tmp_path)
    cli.run_once(pol, state, notifier, tracker, ledger, "box", epath, history=hist,
                 host_tracker=HostTracker())
    return hist.box


def test_watch_records_cpu_ram_and_survives_a_failing_probe(monkeypatch, tmp_path, capsys):
    from tests.test_host import _p, _sample
    box = _run(monkeypatch, tmp_path, lambda: _sample(T0, 0, 0, [_p(1, "alice", 0, 4)]))
    assert [u.user for u in box.users] == ["alice"]

    def broken():
        raise PermissionError("/proc/secret")
    assert _run(monkeypatch, tmp_path, broken) is None
    out = capsys.readouterr().out
    assert "cpu/ram probe failed: PermissionError" in out and "secret" not in out
