"""Notice: how long an important booking waits before it takes a running lendable card."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import DEFAULT_CARD_MIB, GPU_COUNT, ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers

H = timedelta(hours=1)
CARDS = {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}
GRACE = timedelta(minutes=30)


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _lendable(store, *, notice=None, gib=79, hours=4, now=T0):
    return store.create(user="cwinkelmann", gpu=4, vram_mib=gib * 1024, start=now,
                        end=now + timedelta(hours=hours), note=None, ip="10.0.0.1",
                        now=now, priority="lendable", notice_seconds=notice,
                        cards=CARDS).booking


def _important(store, *, now, watching=lambda _id: False, gib=79, hours=2):
    return store.create(user="dorian.zwanzig", gpu=4, vram_mib=gib * 1024, start=now,
                        end=now + timedelta(hours=hours), note=None, ip="10.0.0.2",
                        now=now, priority="important", grace=GRACE, watching=watching,
                        cards=CARDS)


def test_notice_seconds_is_stored_on_the_row(store):
    b = _lendable(store, notice=0)
    assert store.get(b.id).notice_seconds == 0


def test_an_unwatched_victim_keeps_the_full_grace(store):
    _lendable(store, notice=0)                       # declared, but nobody is listening
    made = _important(store, now=T0 + timedelta(minutes=5))
    assert made.booking.start == T0 + timedelta(minutes=35)


def test_a_victim_with_no_declared_notice_keeps_the_full_grace(store):
    victim = _lendable(store, notice=None)
    made = _important(store, now=T0 + timedelta(minutes=5),
                      watching=lambda i: i == victim.id)
    assert made.booking.start == T0 + timedelta(minutes=35)


def test_a_watched_victim_declaring_zero_is_taken_at_once(store):
    victim = _lendable(store, notice=0)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now
    assert made.adjusted_start is None


def test_a_watched_victim_declaring_two_minutes_gets_two_minutes(store):
    victim = _lendable(store, notice=120)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now + timedelta(minutes=2)


def test_a_declared_notice_may_only_shorten_never_lengthen(store):
    victim = _lendable(store, notice=86400)          # "give me a day"
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, watching=lambda i: i == victim.id)
    assert made.booking.start == now + GRACE         # clamped to grace_minutes


def test_the_longest_notice_among_several_victims_wins(store):
    quick = _lendable(store, gib=40, notice=0)
    slow = store.create(user="andre.kliem", gpu=4, vram_mib=39 * 1024, start=T0,
                        end=T0 + timedelta(hours=4), note=None, ip="10.0.0.3", now=T0,
                        priority="lendable", notice_seconds=300, cards=CARDS).booking
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=79,
                      watching=lambda i: i in (quick.id, slow.id))
    assert made.booking.start == now + timedelta(minutes=5)


def test_a_future_victim_is_not_running_so_there_is_no_notice_at_all(store):
    store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024,
                 start=T0 + timedelta(hours=2), end=T0 + timedelta(hours=4), note=None,
                 ip="10.0.0.1", now=T0, priority="lendable", notice_seconds=None,
                 cards=CARDS)
    made = _important(store, now=T0, hours=1)
    assert made.booking.start == T0           # nothing is running; grace does not apply


def test_grace_zero_in_policy_still_means_instant(store):
    victim = _lendable(store, notice=None)
    made = store.create(user="dorian.zwanzig", gpu=4, vram_mib=79 * 1024,
                        start=T0 + timedelta(minutes=5),
                        end=T0 + timedelta(hours=2), note=None, ip="10.0.0.2",
                        now=T0 + timedelta(minutes=5), priority="important",
                        grace=timedelta(0), watching=lambda i: i == victim.id, cards=CARDS)
    assert made.booking.start == T0 + timedelta(minutes=5)
