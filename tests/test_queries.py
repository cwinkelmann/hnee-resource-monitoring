from datetime import date, datetime, timedelta, timezone

import pytest

from resourcemonitor.queries import NoHistory, latest, open_ro, timeline, timeseries, usage
from tests.history_fixture import build_history

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
ASSIGN = {"dorian.zwanzig": frozenset({0, 1, 2, 3}), "cwinkelmann": frozenset({4, 5, 6, 7})}


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
    d = latest(open_ro(db), ASSIGN, now, stale_after_s=600)
    assert d["stale"] is False and len(d["gpus"]) == 8
    g6 = d["gpus"][6]
    assert g6["assigned_to"] == "cwinkelmann"
    assert {p["user"] for p in g6["procs"]} == {"dorian.zwanzig"}
    assert d["gpus"][4]["procs"][0]["user"] is None          # unattributed stays null
    assert any(a["kind"] == "allocation" for a in d["alerts"])


def test_latest_is_stale_when_the_monitor_stopped(db):
    d = latest(open_ro(db), ASSIGN, T0 + timedelta(days=3), stale_after_s=600)
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
    assert len(tl["gpus"]["7"]) == 2                          # one pid, but the 6 h gap splits it


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
