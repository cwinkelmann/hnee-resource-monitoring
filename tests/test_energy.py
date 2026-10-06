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


def test_unattributed_holder_energy_goes_to_its_own_bucket():
    """A None-owner holder must not make its card's energy vanish."""
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 20000, "a"), GpuProcess(2, 6, 10000, None)]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    t = led.totals()
    assert round(t["per_user"]["a"], 2) == 0.40
    assert round(t["unattributed_kwh"], 2) == 0.20
    total = sum(t["per_user"].values()) + t["idle_kwh"] + t["unattributed_kwh"]
    assert round(total, 6) == round(t["per_gpu"][6], 6)


def test_all_none_holders_still_account_for_the_card():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 30000, None)]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert round(led.totals()["unattributed_kwh"], 3) == 0.600


def test_ledger_round_trips_and_old_files_default_unattributed(tmp_path):
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 30000, None)]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))
    f = tmp_path / "e.json"
    led.save(f)
    assert round(EnergyLedger.load(f).totals()["unattributed_kwh"], 3) == 0.600

    f.write_text('{"per_gpu": {"0": 1.0}, "per_user": {}, "idle_kwh": 0.5, "since": "x"}')
    assert EnergyLedger.load(f).totals()["unattributed_kwh"] == 0.0


def test_report_shows_unattributed_and_per_gpu_total():
    txt = format_report({"per_gpu": {0: 1.0, 6: 2.0}, "per_user": {"a": 1.0},
                         "idle_kwh": 0.5, "unattributed_kwh": 1.5,
                         "since": T0.isoformat()}, price_per_kwh=0.30)

    assert "unattributed" in txt.lower()
    assert "1.5 kWh" in txt
    assert "3.0 kWh" in txt          # per-GPU total


from resourcemonitor.energy import EnergyRow, IDLE_BUCKET, UNATTRIBUTED_BUCKET


def test_accumulate_returns_none_and_no_rows_without_an_interval():
    led = EnergyLedger()
    assert led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 0, 70.0)])) == (None, [])


def test_accumulate_returns_one_row_per_gpu_and_bucket():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0), GpuState(0, 81559, 0, 0, 66.0)]
    p = [GpuProcess(1, 6, 10000, "a"), GpuProcess(2, 6, 10000, "a"),
         GpuProcess(3, 6, 10000, None)]
    led.accumulate(_snap(T0, g, p))

    dt, rows = led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert dt == 3600.0
    by = {(r.gpu, r.bucket): r for r in rows}
    assert set(by) == {(6, "a"), (6, UNATTRIBUTED_BUCKET), (0, IDLE_BUCKET)}
    assert round(by[(6, "a")].kwh, 3) == 0.400            # two procs merged: 2/3 of 0.6
    assert round(by[(6, UNATTRIBUTED_BUCKET)].kwh, 3) == 0.200
    assert round(by[(0, IDLE_BUCKET)].kwh, 3) == 0.066
    assert all(r.seconds == 3600.0 for r in rows)


def test_rows_sum_to_the_same_energy_the_ledger_books():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 20000, "a"), GpuProcess(2, 6, 10000, "b")]
    led.accumulate(_snap(T0, g, p))
    _, rows = led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert round(sum(r.kwh for r in rows), 6) == round(led.totals()["per_gpu"][6], 6)


def test_a_gap_returns_no_rows():
    led = EnergyLedger(max_gap_s=300)
    g = [GpuState(0, 81559, 0, 100, 700.0)]
    led.accumulate(_snap(T0, g))
    assert led.accumulate(_snap(T0 + timedelta(hours=6), g)) == (None, [])
