import sqlite3
from datetime import datetime, timedelta, timezone

from resourcemonitor.energy import EnergyRow
from resourcemonitor.history import HistoryWriter
from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.rules import Alert

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _snap(t):
    return Snapshot(t, (GpuState(6, 81559, 22715, 100, 575.0),),
                    (GpuProcess(42, 6, 22706, "dorian.zwanzig", "python kev_run.py"),
                     GpuProcess(43, 6, 9, None, None)))


def _rows(db, sql):
    return sqlite3.connect(db).execute(sql).fetchall()


def test_record_writes_one_transaction_across_all_tables(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db)
    a = Alert(kind="allocation", key="allocation:dorian.zwanzig:6", text="x", gpu_index=6,
              user="dorian.zwanzig")
    w.record(_snap(T0), 60.0, [EnergyRow(6, "dorian.zwanzig", 0.0096, 60.0)], [a], {a.key})
    w.close()

    assert _rows(db, "SELECT ts, dt_s FROM polls") == [(T0.isoformat(), 60.0)]
    assert _rows(db, "SELECT gpu, used_mib, util_pct, power_w FROM gpu_samples") == [(6, 22715, 100, 575.0)]
    assert sorted(_rows(db, "SELECT pid, user, name FROM proc_samples"), key=lambda r: r[0]) == \
        [(42, "dorian.zwanzig", "python kev_run.py"), (43, None, None)]
    assert _rows(db, "SELECT bucket, kwh, seconds FROM energy_samples") == [("dorian.zwanzig", 0.0096, 60.0)]
    assert _rows(db, "SELECT kind, gpu, user, sent FROM alerts") == [("allocation", 6, "dorian.zwanzig", 1)]


def test_first_poll_records_null_interval(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db)
    w.record(_snap(T0), None, [], [], set())
    w.close()
    assert _rows(db, "SELECT dt_s FROM polls") == [(None,)]


def test_db_is_in_wal_mode_so_a_reader_never_blocks_the_writer(tmp_path):
    db = tmp_path / "h.sqlite"
    HistoryWriter(db).close()
    assert _rows(db, "PRAGMA journal_mode") == [("wal",)]


def test_reopening_an_existing_db_keeps_its_rows(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db); w.record(_snap(T0), None, [], [], set()); w.close()
    w = HistoryWriter(db); w.record(_snap(T0 + timedelta(minutes=1)), 60.0, [], [], set()); w.close()
    assert len(_rows(db, "SELECT ts FROM polls")) == 2


def test_prune_drops_rows_older_than_retention(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db, retention_days=1)
    w.record(_snap(T0), None, [EnergyRow(6, "(idle)", 0.1, 60.0)], [], set())
    w.record(_snap(T0 + timedelta(days=2)), None, [], [], set())   # triggers the daily prune
    w.close()
    assert [r[0] for r in _rows(db, "SELECT ts FROM polls")] == [(T0 + timedelta(days=2)).isoformat()]
    assert _rows(db, "SELECT COUNT(*) FROM energy_samples") == [(0,)]


def test_fixture_builds_a_deterministic_history(tmp_path):
    from tests.history_fixture import build_history
    db = tmp_path / "h.sqlite"
    build_history(db, T0, hours=24)
    (n,) = _rows(db, "SELECT COUNT(*) FROM polls")[0]
    assert n == 24 * 12                                   # 5-minute polls
    buckets = {b for (b,) in _rows(db, "SELECT DISTINCT bucket FROM energy_samples")}
    assert buckets == {"dorian.zwanzig", "cwinkelmann", "(idle)", "(unattributed)"}


def test_fixture_has_eight_runs_on_gpu6_and_honours_the_gap(tmp_path):
    from tests.history_fixture import build_history
    db = tmp_path / "h.sqlite"
    gap = (T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    build_history(db, T0, hours=48, gap=gap)
    assert _rows(db, "SELECT COUNT(DISTINCT pid) FROM proc_samples WHERE gpu=6") == [(8,)]
    assert _rows(db, "SELECT COUNT(DISTINCT pid) FROM proc_samples WHERE gpu=7") == [(1,)]
    assert _rows(db, "SELECT COUNT(*) FROM polls") == [(48 * 12 - 12,)]
    assert _rows(db, f"SELECT dt_s FROM polls WHERE ts='{(gap[1]).isoformat()}'") == [(None,)]
