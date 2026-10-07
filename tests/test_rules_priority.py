"""Alert rules against effective shares: lendable bookings squeezed by important ones."""
from datetime import timedelta

from resourcemonitor.claims import Booking
from resourcemonitor.rules import check_bookings, check_takes
from tests.test_rules_bookings import GIB, POL, T0, _p, _snap

H = timedelta(hours=1)


def _b(id, user, gib, priority, start=T0 - H, hours=6, gpu=4, cancelled=False):
    return Booking(id=id, user=user, gpu=gpu, vram_mib=gib * GIB, start=start,
                   end=start + hours * H, note=None, created_at=start, created_ip="x",
                   cancelled_at=start if cancelled else None, priority=priority)


def test_over_booking_uses_the_effective_share_and_says_why():
    # 81559 MiB card: bob's important 40 GiB leaves alice's lendable 79 GiB with 39.6 GiB.
    bs = [_b(1, "alice", 79, "lendable"), _b(2, "bob", 40, "important")]
    alerts = check_bookings(_snap([_p("alice", 70 * GIB), _p("bob", 10 * GIB, pid=2)]), POL, bs)
    (a,) = alerts
    assert (a.kind, a.key, a.user) == ("over_booking", "booking:over:alice:4", "alice")
    assert "alice uses 70.0 GiB on GPU 4" in a.text
    assert "lendable booking was reduced from 79.0 GiB to 39.6 GiB by bob's important booking" \
        in a.text


def test_within_effective_share_is_quiet():
    bs = [_b(1, "alice", 79, "lendable"), _b(2, "bob", 40, "important")]
    assert check_bookings(_snap([_p("alice", 39 * GIB)]), POL, bs) == []


def test_unbooked_users_get_the_remainder_after_effective_shares():
    # alice 30 lendable + bob 40 important = 70 effective; 9.6 GiB left for others.
    bs = [_b(1, "alice", 30, "lendable"), _b(2, "bob", 40, "important")]
    assert check_bookings(_snap([_p("carol", 9 * GIB, pid=3)]), POL, bs) == []
    (a,) = check_bookings(_snap([_p("carol", 10 * GIB, pid=3)]), POL, bs)
    assert a.kind == "booked_gpu" and "9.6 GiB unbooked" in a.text


def test_taken_heads_up_fires_during_grace_period_once_per_pair():
    now = T0
    lend = _b(1, "alice", 79, "lendable", start=now - H, hours=6)
    imp = _b(2, "bob", 40, "important", start=now + timedelta(minutes=30), hours=2)
    (a,) = check_takes(now, [lend, imp], {4: 81559})
    assert (a.kind, a.key, a.user, a.gpu_index) == ("taken", "booking:taken:2:1", "alice", 4)
    assert a.text == ("bob takes 39.4 GiB of alice's lendable booking on GPU 4 "
                      "from Tue 12:30 until Tue 14:30 UTC.")


def test_taken_ignores_finished_cancelled_and_harmless_bookings():
    now = T0
    lend = _b(1, "alice", 70, "lendable", start=now - 3 * H, hours=6)
    past = _b(2, "bob", 40, "important", start=now - 3 * H, hours=1)
    gone = _b(3, "bob", 40, "important", start=now + H, hours=1, cancelled=True)
    small = _b(4, "carol", 1, "important", start=now + H, hours=1)
    assert check_takes(now, [lend, past, gone, small], {4: 81559}) == []


def test_taken_skips_gpus_missing_from_the_snapshot():
    lend = _b(1, "alice", 79, "lendable")
    imp = _b(2, "bob", 40, "important", start=T0 + H)
    assert check_takes(T0, [lend, imp], {}) == []
