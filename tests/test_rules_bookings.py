from datetime import datetime, timedelta, timezone

from resourcemonitor.claims import Booking
from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import check_bookings, check_unattributed

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
POL = Policy(assignments={}, idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
             capacity_free_mib=40960, cooldown_s=3600, channel="#c")
GIB = 1024


def _b(user, gpu=4, gib=40, start=T0 - timedelta(hours=1), hours=6, cancelled=False, id=1):
    return Booking(id=id, user=user, gpu=gpu, vram_mib=gib * GIB, start=start,
                   end=start + timedelta(hours=hours), note=None, created_at=start,
                   created_ip="10.0.0.1",
                   cancelled_at=start if cancelled else None,
                   cancelled_ip="10.0.0.2" if cancelled else None)


def _snap(procs, t=T0, gpu=4):
    return Snapshot(t, (GpuState(gpu, 81559, sum(p.used_mib for p in procs), 90),), tuple(procs))


def _p(user, mib, pid=1, gpu=4):
    return GpuProcess(pid, gpu, mib, user)


def test_unbooked_gpu_is_free_for_anyone():
    assert check_bookings(_snap([_p("andre.kliem", 70 * GIB)]), POL, []) == []


def test_within_own_share_is_fine_and_110_percent_is_still_fine():
    b = [_b("dorian.zwanzig", gib=40)]
    assert check_bookings(_snap([_p("dorian.zwanzig", 44 * GIB)]), POL, b) == []   # exactly 110 %


def test_over_own_share_beyond_tolerance_alerts():
    b = [_b("dorian.zwanzig", gib=40)]
    (a,) = check_bookings(_snap([_p("dorian.zwanzig", 44 * GIB + 1)]), POL, b)
    assert (a.kind, a.key, a.user, a.gpu_index) == ("over_booking", "booking:over:dorian.zwanzig:4", "dorian.zwanzig", 4)
    assert "booked 40.0 GiB" in a.text


def test_non_booker_within_the_unbooked_remainder_is_fine():
    b = [_b("dorian.zwanzig", gib=40)]
    procs = [_p("dorian.zwanzig", 30 * GIB), _p("andre.kliem", 39 * GIB, pid=2)]
    assert check_bookings(_snap(procs), POL, b) == []


def test_non_bookers_together_beyond_the_remainder_all_alert():
    b = [_b("dorian.zwanzig", gib=60)]
    procs = [_p("andre.kliem", 12 * GIB, pid=2), _p("cwinkelmann", 10 * GIB, pid=3)]
    alerts = check_bookings(_snap(procs), POL, b)
    assert {(a.kind, a.user) for a in alerts} == {("booked_gpu", "andre.kliem"), ("booked_gpu", "cwinkelmann")}
    assert all("booked by dorian.zwanzig" in a.text and "unbooked" in a.text for a in alerts)


def test_a_user_with_two_processes_is_one_incident_with_summed_vram():
    b = [_b("dorian.zwanzig", gib=40)]
    procs = [_p("dorian.zwanzig", 30 * GIB), _p("dorian.zwanzig", 20 * GIB, pid=2)]
    (a,) = check_bookings(_snap(procs), POL, b)
    assert a.kind == "over_booking" and "50.0 GiB" in a.text


def test_two_bookings_by_one_user_add_up():
    b = [_b("dorian.zwanzig", gib=20, id=1), _b("dorian.zwanzig", gib=20, id=2)]
    assert check_bookings(_snap([_p("dorian.zwanzig", 40 * GIB)]), POL, b) == []


def test_expired_and_cancelled_bookings_are_ignored():
    expired = _b("dorian.zwanzig", gib=79, start=T0 - timedelta(hours=5), hours=5)   # ends exactly at T0
    cancelled = _b("cwinkelmann", gib=79, cancelled=True, id=2)
    assert check_bookings(_snap([_p("andre.kliem", 70 * GIB)]), POL, [expired, cancelled]) == []


def test_alert_stops_at_the_first_poll_after_the_booking_ends():
    b = [_b("dorian.zwanzig", gib=79, start=T0 - timedelta(hours=1), hours=2)]
    busy = [_p("andre.kliem", 30 * GIB)]
    assert check_bookings(_snap(busy, t=T0), POL, b)                                 # booked: alert
    assert check_bookings(_snap(busy, t=T0 + timedelta(hours=1)), POL, b) == []      # ended: free


def test_unattributed_processes_never_trigger_booking_alerts():
    b = [_b("dorian.zwanzig", gib=79)]
    assert check_bookings(_snap([GpuProcess(9, 4, 30 * GIB, None)]), POL, b) == []


def test_unattributed_rule_fires_on_booked_gpus_only():
    procs = [GpuProcess(9, 4, 2 * GIB, None)]
    assert check_unattributed(_snap(procs), POL, []) == []
    (a,) = check_unattributed(_snap(procs), POL, [_b("dorian.zwanzig")])
    assert a.kind == "unattributed" and "booked by dorian.zwanzig" in a.text


def test_unattributed_processes_on_a_booked_gpu_are_summed_into_one_alert():
    procs = [GpuProcess(20, 4, 2048, None), GpuProcess(21, 4, 4096, None)]
    (a,) = check_unattributed(_snap(procs), POL, [_b("dorian.zwanzig")])
    assert a.key == "unattributed:4" and a.user is None and a.gpu_index == 4
    assert "holding 6.0 GiB on GPU 4" in a.text and "could not be resolved" in a.text


def test_unattributed_ignores_small_and_attributed_processes():
    procs = [GpuProcess(1, 4, 100, None),               # below idle_min_mib
             GpuProcess(2, 4, 5000, "andre.kliem")]     # attributed
    assert check_unattributed(_snap(procs), POL, [_b("dorian.zwanzig")]) == []


def test_unattributed_alerts_only_on_the_booked_gpu():
    snap = Snapshot(T0, (GpuState(4, 81559, 4096, 0), GpuState(5, 81559, 4096, 0)),
                    (GpuProcess(1, 4, 4096, None), GpuProcess(2, 5, 4096, None)))
    (a,) = check_unattributed(snap, POL, [_b("dorian.zwanzig")])
    assert a.key == "unattributed:4"


def test_booked_gpu_alert_key_and_full_text_with_two_bookers():
    b = [_b("zed", gib=20, hours=5, id=1),                 # ends T0 + 4h
         _b("amy", gib=30, hours=3, id=2)]                 # ends T0 + 2h = Tue 14:00
    (a,) = check_bookings(_snap([_p("andre.kliem", 35 * GIB)]), POL, b)
    assert a.kind == "booked_gpu" and a.key == "booking:other:andre.kliem:4"
    assert a.text == ("andre.kliem is using 35.0 GiB on GPU 4; 50.0 GiB is booked by "
                      "amy, zed until Tue 14:00 UTC, 29.6 GiB unbooked.")


def test_bookings_are_per_gpu():
    b = [_b("dorian.zwanzig", gpu=4, gib=40)]
    snap = Snapshot(T0, (GpuState(4, 81559, 0, 0), GpuState(5, 81559, 70 * GIB, 90)),
                    (GpuProcess(1, 5, 70 * GIB, "dorian.zwanzig"),))
    assert check_bookings(snap, POL, b) == []


def test_over_booker_and_crowding_non_booker_are_both_reported():
    b = [_b("dorian.zwanzig", gib=40)]
    procs = [_p("dorian.zwanzig", 50 * GIB), _p("andre.kliem", 45 * GIB, pid=2)]
    alerts = check_bookings(_snap(procs), POL, b)
    assert {(a.kind, a.key) for a in alerts} == {
        ("over_booking", "booking:over:dorian.zwanzig:4"),
        ("booked_gpu", "booking:other:andre.kliem:4")}
