from datetime import datetime, timedelta, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import IdleTracker

POL = Policy(assignments={}, idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
             capacity_free_mib=40960, cooldown_s=3600, channel="#c")
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

def _snap(t, util, mib=20000, user="someone", pid=42):
    return Snapshot(t, (GpuState(3, 81559, mib, util),),
                    (GpuProcess(pid, 3, mib, user),))

def test_silent_before_the_grace_period_elapses():
    tr = IdleTracker()
    assert tr.observe(_snap(T0, util=0), POL) == []
    assert tr.observe(_snap(T0 + timedelta(minutes=29), util=0), POL) == []

def test_fires_once_the_grace_period_elapses():
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)

    alerts = tr.observe(_snap(T0 + timedelta(minutes=31), util=0), POL)

    assert len(alerts) == 1
    assert alerts[0].kind == "idle"
    assert "30" in alerts[0].text or "31" in alerts[0].text

def test_activity_resets_the_timer():
    """A job between epochs dips to 0% briefly. That must not accumulate."""
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)
    tr.observe(_snap(T0 + timedelta(minutes=20), util=90), POL)      # busy again

    assert tr.observe(_snap(T0 + timedelta(minutes=35), util=0), POL) == []

def test_a_small_allocation_is_not_worth_flagging():
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0, mib=200), POL)

    assert tr.observe(_snap(T0 + timedelta(minutes=31), util=0, mib=200), POL) == []

def test_a_vanished_process_is_forgotten():
    """Otherwise the tracker leaks an entry per PID that ever ran."""
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)
    empty = Snapshot(T0 + timedelta(minutes=5), (GpuState(3, 81559, 0, 0),), ())

    tr.observe(empty, POL)

    assert tr.tracked() == 0
