from datetime import datetime, timedelta, timezone

from resourcemonitor.energy import EnergyLedger, format_report
from resourcemonitor.model import GpuProcess, GpuState, Snapshot

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _snap(t, gpus, procs=()):
    return Snapshot(t, tuple(gpus), tuple(procs))


def test_first_snapshot_accumulates_nothing():
    """There is no interval yet; integrating from nothing invents energy."""
    led = EnergyLedger()
    led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 0, 700.0)]))

    assert led.totals()["per_gpu"].get(0, 0.0) == 0.0


def test_one_hour_at_700w_is_0_7_kwh():
    led = EnergyLedger(max_gap_s=7200)
    led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 100, 700.0)]))
    led.accumulate(_snap(T0 + timedelta(hours=1), [GpuState(0, 81559, 0, 100, 700.0)]))

    assert round(led.totals()["per_gpu"][0], 3) == 0.700


def test_energy_follows_the_user_holding_the_card():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 22715, 100, 600.0)]
    p = [GpuProcess(1, 6, 22715, "dorian.zwanzig")]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert round(led.totals()["per_user"]["dorian.zwanzig"], 3) == 0.600


def test_two_users_on_one_card_split_by_memory_share():
    """power.draw is per CARD. Splitting by memory is a proxy and is labelled as one."""
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 20000, "a"), GpuProcess(2, 6, 10000, "b")]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    tot = led.totals()["per_user"]
    assert round(tot["a"], 2) == 0.40
    assert round(tot["b"], 2) == 0.20


def test_idle_energy_is_attributed_to_nobody_not_dropped():
    """An idle card still burns ~66 W. That is the number the idle rule exists for."""
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(0, 81559, 0, 0, 66.0)]
    led.accumulate(_snap(T0, g))
    led.accumulate(_snap(T0 + timedelta(hours=1), g))

    t = led.totals()
    assert round(t["idle_kwh"], 3) == 0.066
    assert t["per_user"] == {}


def test_a_long_gap_is_not_integrated():
    """After a restart the last reading is stale; integrating it invents kWh."""
    led = EnergyLedger(max_gap_s=300)
    g = [GpuState(0, 81559, 0, 100, 700.0)]
    led.accumulate(_snap(T0, g))
    led.accumulate(_snap(T0 + timedelta(hours=6), g))

    assert led.totals()["per_gpu"].get(0, 0.0) == 0.0


def test_report_states_that_it_measures_only_uptime():
    txt = format_report({"per_gpu": {0: 1.0}, "per_user": {"a": 1.0},
                         "idle_kwh": 0.5, "since": T0.isoformat()}, price_per_kwh=0.30)

    assert "0.30" in txt or "€" in txt
    assert "while the monitor was running" in txt.lower() or "uptime" in txt.lower()
