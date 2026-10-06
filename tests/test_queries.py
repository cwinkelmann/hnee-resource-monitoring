from datetime import date, datetime, timedelta, timezone

import pytest

from resourcemonitor.queries import (NoHistory, latest, open_ro, timeline, timeseries, usage,
                                     vram_timeseries)
from tests.history_fixture import build_history

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    p = tmp_path_factory.mktemp("gap") / "h.sqlite"
    build_history(p, T0, hours=48,
                  gap=(T0 + timedelta(hours=30), T0 + timedelta(hours=36)))
    return p


@pytest.fixture(scope="module")
def db_nogap(tmp_path_factory):
    p = tmp_path_factory.mktemp("nogap") / "h.sqlite"
    build_history(p, T0, hours=48)
    return p


def test_missing_file_is_no_history_not_a_crash(tmp_path):
    with pytest.raises(NoHistory):
        open_ro(tmp_path / "absent.sqlite")


def test_reader_cannot_write(db):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        open_ro(db).execute("DELETE FROM polls")


def test_latest_reports_gpus_owners_and_assignees(db):
    now = T0 + timedelta(hours=48)
    d = latest(open_ro(db), [], now, stale_after_s=600)
    assert d["stale"] is False and len(d["gpus"]) == 8
    g6 = d["gpus"][6]
    assert "assigned_to" not in g6 and g6["bookings"] == []
    assert g6["booked_mib"] == 0 and g6["free_mib"] == g6["total_mib"]
    assert {p["user"] for p in g6["procs"]} == {"dorian.zwanzig"}
    assert d["gpus"][4]["procs"][0]["user"] is None          # unattributed stays null
    assert [a["key"] for a in d["alerts"]] == [               # one alert per incident
        "booking:other:dorian.zwanzig:6", "booking:other:dorian.zwanzig:7", "unattributed:4"]
    assert "using 22.7 GiB on GPU 7" in d["alerts"][1]["text"]      # job + helper, summed


def test_latest_reports_active_bookings_per_gpu(db):
    from resourcemonitor.claims import Booking
    now = T0 + timedelta(hours=48)
    def bk(i, gpu, mib, start):
        return Booking(id=i, user="cwinkelmann", gpu=gpu, vram_mib=mib, start=start,
                       end=now + timedelta(hours=1), note=None, created_at=now, created_ip="x")
    bookings = [bk(2, 6, 20480, now - timedelta(minutes=5)), bk(1, 6, 40960, now - timedelta(hours=1))]
    d = latest(open_ro(db), bookings, now, 600)
    g6 = d["gpus"][6]
    assert [b["id"] for b in g6["bookings"]] == [1, 2] and g6["bookings"][0]["user"] == "cwinkelmann"
    assert g6["booked_mib"] == 61440 and g6["free_mib"] == g6["total_mib"] - 61440
    big = latest(open_ro(db), [bk(3, 5, 10**6, now)], now, 600)["gpus"][5]
    assert big["free_mib"] == 0


def test_latest_reports_the_slack_mode(tmp_path):
    p = tmp_path / "h.sqlite"
    build_history(p, T0, hours=1, slack_mode="dry-run")
    assert latest(open_ro(p), [], T0 + timedelta(hours=1), 600)["slack"] == "dry-run"


def test_latest_slack_mode_is_null_for_an_old_db_without_meta(tmp_path):
    p = tmp_path / "old.sqlite"
    build_history(p, T0, hours=1)
    import sqlite3
    c = sqlite3.connect(p); c.execute("DROP TABLE meta"); c.commit(); c.close()
    assert latest(open_ro(p), [], T0 + timedelta(hours=1), 600)["slack"] is None


def test_latest_slack_mode_is_null_when_never_recorded(db):
    assert latest(open_ro(db), [], T0 + timedelta(hours=48), 600)["slack"] is None


def test_latest_is_stale_when_the_monitor_stopped(db):
    d = latest(open_ro(db), [], T0 + timedelta(days=3), stale_after_s=600)
    assert d["stale"] is True and d["age_s"] > 600


def test_usage_by_day_has_continuous_periods_and_caveats(db):
    u = usage(open_ro(db), date(2026, 10, 4), date(2026, 10, 7), "day",
              now=T0 + timedelta(hours=48))
    assert [p["start"] for p in u["periods"]] == ["2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"]
    assert u["periods"][0]["kwh"] == {}                      # before monitoring began
    assert u["totals"]["kwh"]["dorian.zwanzig"] > u["totals"]["kwh"]["cwinkelmann"] > 0
    assert "(idle)" in u["totals"]["kwh"] and "(unattributed)" in u["totals"]["kwh"]
    assert len(u["caveats"]) == 2


def test_usage_coverage_reflects_the_gap(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 6), "day",
              now=T0 + timedelta(hours=48))
    assert 0.80 < u["coverage"]["ratio"] < 0.92               # 6 h of 48 missing, nothing invented


def test_usage_coverage_says_since_when_it_is_measured(db):
    now = T0 + timedelta(hours=48)
    inside = usage(open_ro(db), date(2026, 10, 6), date(2026, 10, 6), "day", now=now)
    assert inside["coverage"]["since"] == "2026-10-06"         # range starts after first poll
    wide = usage(open_ro(db), date(2026, 9, 7), date(2026, 10, 6), "day", now=now)
    assert wide["coverage"]["since"] == "2026-10-05"           # monitoring began mid-range
    for lo, hi in ((date(2026, 9, 1), date(2026, 9, 2)), (date(2026, 10, 10), date(2026, 10, 11))):
        empty = usage(open_ro(db), lo, hi, "day", now=now)
        assert empty["coverage"]["elapsed_s"] == 0 and empty["coverage"]["since"] is None


def test_usage_by_week_merges_days(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 6), "week",
              now=T0 + timedelta(hours=48))
    assert [p["start"] for p in u["periods"]] == ["2026-10-05"]   # Monday


def test_gpu_hours_for_two_full_gpus_over_a_day(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 5), "day",
              now=T0 + timedelta(hours=48))
    assert 47.0 < u["totals"]["gpu_hours"]["dorian.zwanzig"] <= 48.0   # GPUs 6+7 × 24 h


def test_timeline_shows_who_did_what_when(db_nogap):
    tl = timeline(open_ro(db_nogap), hours=48, now=T0 + timedelta(hours=48))
    g6 = tl["gpus"]["6"]
    assert len(g6) == 8                                      # new pid every 6 h
    assert {(j["user"], j["name"]) for j in g6} == {("dorian.zwanzig", "python kev_run.py")}
    assert [j["ongoing"] for j in g6] == [False] * 7 + [True]
    assert all(j["start"] < j["end"] for j in g6)
    assert tl["gpus"]["4"][0]["user"] is None                # unattributed stays null
    assert tl["gpus"]["0"] == []                              # idle GPU present, no jobs


def test_timeline_splits_a_job_at_a_monitoring_gap(db):
    tl = timeline(open_ro(db), hours=48, now=T0 + timedelta(hours=48))
    g7 = tl["gpus"]["7"]                                     # two pids, each split by the 6 h gap
    assert sorted(j["pid"] for j in g7) == [2000, 2000, 2001, 2001]
    gap_start, gap_end = (T0 + timedelta(hours=30)).isoformat(), (T0 + timedelta(hours=36)).isoformat()
    for pid in (2000, 2001):
        first, second = sorted((j for j in g7 if j["pid"] == pid), key=lambda j: j["start"])
        assert first["end"] < gap_start and second["start"] == gap_end


def test_timeline_window_excludes_old_jobs(db):
    tl = timeline(open_ro(db), hours=1, now=T0 + timedelta(hours=48))
    assert len(tl["gpus"]["6"]) == 1


def test_timeseries_is_downsampled(db):
    ts = timeseries(open_ro(db), hours=48, now=T0 + timedelta(hours=48), max_points=50)
    assert set(ts["gpus"]) == {str(i) for i in range(8)}
    assert all(len(v) <= 50 for v in ts["gpus"].values())


def test_timeline_run_start_looks_back_before_the_window(db_nogap):
    # window opens 1 h before the end; the pid on GPU 6 began at hour 42
    tl = timeline(open_ro(db_nogap), hours=1, now=T0 + timedelta(hours=48))
    (job,) = tl["gpus"]["6"]
    assert job["start"] == (T0 + timedelta(hours=42)).isoformat()
    assert job["ongoing"] is True
    assert job["max_mib"] == 22715


def test_open_ro_stays_read_only_for_paths_with_uri_characters(tmp_path):
    import sqlite3
    from resourcemonitor.history import HistoryWriter
    d = tmp_path / "a?b#c%d"
    d.mkdir()
    HistoryWriter(d / "h.sqlite").close()
    with pytest.raises(sqlite3.OperationalError):
        open_ro(d / "h.sqlite").execute("CREATE TABLE x(a)")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a?b#c%d"]


def test_usage_rejects_unknown_period(db):
    with pytest.raises(ValueError):
        usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 6), "month", now=T0)


def _mk(path, polls):
    """polls: [(minute, dt_s, [(gpu, pid, mib)])] written straight into the schema."""
    import sqlite3
    from resourcemonitor.history import _SCHEMA
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    for m, dt, procs in polls:
        ts = (T0 + timedelta(minutes=m)).isoformat()
        c.execute("INSERT INTO polls VALUES (?, ?)", (ts, dt))
        for g in range(2):
            c.execute("INSERT INTO gpu_samples VALUES (?, ?, 1, 1, 1, 1.0)", (ts, g))
        for g, pid, mib in procs:
            c.execute("INSERT INTO proc_samples VALUES (?, ?, ?, 'u', ?, 'n')", (ts, g, pid, mib))
    c.commit()
    c.close()


def _runs(path, hours, now_min):
    tl = timeline(open_ro(path), hours, T0 + timedelta(minutes=now_min))
    return {g: [(j["pid"], j["start"][11:16], j["end"][11:16], j["max_mib"], j["ongoing"])
                for j in js] for g, js in tl["gpus"].items()}


@pytest.fixture(scope="module")
def edge_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("edge") / "e.sqlite"
    _mk(p, [(0, None, [(0, 5, 100)]), (1, 60, [(0, 5, 900)]), (2, 60, [(0, 5, 100)]),
            (3, None, [(0, 5, 100)]), (4, 60, [(0, 5, 100)]),
            (5, 60, [(1, 5, 100)]), (6, 60, []), (7, 60, [(1, 5, 50)])])
    return p


def test_timeline_edge_semantics(edge_db):
    r = _runs(edge_db, 10, 8)
    # gap poll at 00:03 splits the first pid-5 run; GPU 1 is a separate run; a
    # pid that vanishes (06) and returns (07) makes two runs
    assert r["0"] == [(5, "00:00", "00:02", 900, False), (5, "00:03", "00:04", 100, False)]
    assert r["1"] == [(5, "00:05", "00:05", 100, False), (5, "00:07", "00:07", 50, True)]


def test_timeline_lookback_stops_at_gap_and_takes_max_from_before_window(edge_db):
    # window opens at 00:04; run starts at the gap poll 00:03 (it contains the pid)
    assert _runs(edge_db, 1, 64)["0"] == [(5, "00:03", "00:04", 100, False)]
    # window opens at 00:02: look-back reaches 00:01 (max 900) and stops at 00:00 gap poll
    assert _runs(edge_db, 1, 62)["0"][0] == (5, "00:00", "00:02", 900, False)


def test_usage_total_gpu_hours_counts_a_shared_gpu_once(tmp_path):
    """User + unattributed process on one card for 10 min: each bucket held it 10 min,
    but the card was busy 10 min, not 20."""
    from resourcemonitor.energy import EnergyLedger
    from resourcemonitor.history import HistoryWriter
    from resourcemonitor.model import GpuProcess, GpuState, Snapshot
    p = tmp_path / "h.sqlite"
    w, led = HistoryWriter(p), EnergyLedger(max_gap_s=600)
    for m in range(11):
        snap = Snapshot(T0 + timedelta(minutes=m), (GpuState(0, 81559, 6000, 90, 400.0),),
                        (GpuProcess(1, 0, 4000, "a", "x"), GpuProcess(2, 0, 2000, None, None)))
        dt, rows = led.accumulate(snap)
        w.record(snap, dt, rows, [], set())
    w.close()
    u = usage(open_ro(p), date(2026, 10, 5), date(2026, 10, 5), "day", now=T0 + timedelta(hours=1))
    assert u["totals"]["gpu_hours"] == pytest.approx({"a": 10 / 60, "(unattributed)": 10 / 60})
    assert u["gpu_hours_total"] == pytest.approx(10 / 60)


def test_usage_total_gpu_hours_on_the_fixture_is_all_cards(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 5), "day",
              now=T0 + timedelta(hours=48))
    assert u["gpu_hours_total"] == pytest.approx(8 * (24 - 5 / 60), abs=1e-6)  # 8 cards; first poll has no interval


@pytest.mark.parametrize("fn", ["usage", "timeline", "timeseries", "vram_timeseries"])
def test_queries_on_a_db_without_polls_are_no_history(tmp_path, fn):
    from resourcemonitor.history import HistoryWriter
    p = tmp_path / "h.sqlite"
    HistoryWriter(p).close()
    now = T0 + timedelta(hours=1)
    call = {"usage": lambda c: usage(c, date(2026, 10, 5), date(2026, 10, 5), "day", now),
            "timeline": lambda c: timeline(c, 24, now),
            "timeseries": lambda c: timeseries(c, 24, now),
            "vram_timeseries": lambda c: vram_timeseries(c, 24, now)}[fn]
    with pytest.raises(NoHistory):
        call(open_ro(p))


def _vram(db, hours=48, max_points=10**6):
    return vram_timeseries(open_ro(db), hours=hours, now=T0 + timedelta(hours=48),
                           max_points=max_points)


def test_vram_buckets_per_gpu_match_the_fixture(db):
    v = _vram(db)
    assert v["to"] == (T0 + timedelta(hours=48)).isoformat() and v["from"] == T0.isoformat()
    buckets = {g: set().union(*(p["by_user"] for p in d["points"])) for g, d in v["gpus"].items()}
    assert buckets == {"0": set(), "1": set(), "2": set(), "3": set(), "4": {"(unattributed)"},
                       "5": {"cwinkelmann"}, "6": {"dorian.zwanzig"}, "7": {"dorian.zwanzig"}}
    assert all(d["total_mib"] == 81559 for d in v["gpus"].values())
    g7 = v["gpus"]["7"]["points"]
    assert len(g7) == 42 * 12                                # 48 h of 5-min polls minus the 6 h gap
    assert [p["ts"] for p in g7] == sorted(p["ts"] for p in g7)
    assert g7[0]["by_user"] == {"dorian.zwanzig": 22715 + 512}       # job + helper, summed
    assert v["gpus"]["4"]["points"][0]["by_user"] == {"(unattributed)": 2048}


def test_vram_point_sums_equal_the_proc_samples_of_that_poll(db):
    import sqlite3
    v = _vram(db)
    c = sqlite3.connect(db)
    for gpu in ("4", "5", "7"):
        for p in v["gpus"][gpu]["points"][::97]:
            (want,) = c.execute("SELECT COALESCE(SUM(used_mib), 0) FROM proc_samples "
                                "WHERE ts = ? AND gpu = ?", (p["ts"], int(gpu))).fetchone()
            assert sum(p["by_user"].values()) == want
    c.close()


def test_vram_polls_without_processes_are_empty_points(db):
    v = _vram(db)
    assert all(p["by_user"] == {} for p in v["gpus"]["0"]["points"])
    g5 = {p["ts"]: p["by_user"] for p in v["gpus"]["5"]["points"]}
    assert g5[(T0 + timedelta(hours=1)).isoformat()] == {}               # odd hour: idle
    assert g5[(T0 + timedelta(hours=2)).isoformat()] == {"cwinkelmann": 12000}


def test_vram_is_downsampled_by_averaging_with_missing_buckets_as_zero(db):
    v = _vram(db, max_points=50)
    assert all(0 < len(d["points"]) <= 50 for d in v["gpus"].values())
    g5 = [p["by_user"].get("cwinkelmann", 0) for p in v["gpus"]["5"]["points"]]
    assert any(0 < x < 12000 for x in g5)                    # chunks span busy and idle hours
    assert all(p["by_user"]["dorian.zwanzig"] == pytest.approx(23227)
               for p in v["gpus"]["7"]["points"])


def test_vram_marks_the_first_point_after_a_monitoring_gap(db):
    v = _vram(db)
    for d in v["gpus"].values():
        assert [p["ts"] for p in d["points"] if p.get("gap")] == [(T0 + timedelta(hours=36)).isoformat()]
    small = _vram(db, max_points=50)
    assert all(sum(1 for p in d["points"] if p.get("gap")) == 1 for d in small["gpus"].values())


def test_vram_window_and_no_gap_flag_without_a_gap(db_nogap):
    v = _vram(db_nogap, hours=1)
    pts = v["gpus"]["6"]["points"]
    assert len(pts) == 12 and not any(p.get("gap") for p in pts)
