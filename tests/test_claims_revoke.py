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


def test_a_starved_victim_is_truncated_at_the_instant_it_loses_the_card(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=79, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == now
    assert store.get(victim.id).cancelled_at is None       # it ran; energy still counts
    assert [r["id"] for r in made.revoked] == [victim.id]
    assert made.revoked[0]["truncated"] is True
    # 79 GiB taken from a 79 GiB booking leaves a 663 MiB crumb, below anything bookable.
    assert made.revoked[0]["effective_mib"] < 1024
    assert made.revoked[0]["by"] == ["dorian.zwanzig"]


def test_a_victim_shrunk_but_not_starved_is_reported_and_left_alone(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    made = _important(store, now=now, gib=40, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == T0 + timedelta(hours=4)
    assert made.revoked[0]["truncated"] is False
    assert made.revoked[0]["effective_mib"] > 0
    assert made.revoked[0]["lost_mib"] > 0


def test_truncation_waits_for_the_notice_rather_than_happening_now(store):
    victim = _lendable(store, notice=120, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id)
    assert store.get(victim.id).end == now + timedelta(minutes=2)


def test_an_important_booking_never_revokes_another_important_one(store):
    first = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=T0,
                         end=T0 + timedelta(hours=4), note=None, ip="10.0.0.1", now=T0,
                         priority="important", cards=CARDS).booking
    with pytest.raises(ClaimError) as e:
        _important(store, now=T0 + timedelta(minutes=5), gib=79)
    assert e.value.status == 409
    assert store.get(first.id).end == T0 + timedelta(hours=4)


def test_a_lendable_booking_revokes_nothing(store):
    victim = _lendable(store, notice=0, gib=40, hours=4)
    made = store.create(user="dorian.zwanzig", gpu=4, vram_mib=39 * 1024, start=T0,
                        end=T0 + H, note=None, ip="10.0.0.2", now=T0,
                        priority="lendable", cards=CARDS)
    assert made.revoked == []
    assert store.get(victim.id).end == T0 + timedelta(hours=4)


def test_revoking_twice_at_the_same_instant_truncates_once(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id, hours=1)
    second = store.create(user="andre.kliem", gpu=4, vram_mib=79 * 1024, start=now + H,
                          end=now + timedelta(hours=2), note=None, ip="10.0.0.3", now=now,
                          priority="important", watching=lambda i: i == victim.id,
                          cards=CARDS)
    assert store.get(victim.id).end == now            # unchanged by the second booking
    assert second.revoked == []


def test_truncation_never_leaves_an_end_before_the_start(store):
    victim = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024,
                          start=T0 + timedelta(minutes=10),
                          end=T0 + timedelta(hours=4), note=None, ip="10.0.0.1", now=T0,
                          priority="lendable", notice_seconds=0, cards=CARDS).booking
    _important(store, now=T0, gib=79, hours=3, watching=lambda i: i == victim.id)
    got = store.get(victim.id)
    assert got.end >= got.start


def test_the_freed_tail_advances_someone_else(store):
    victim = _lendable(store, notice=0, gib=79, hours=4)
    waiting = store.create(user="andre.kliem", gpu=4, vram_mib=79 * 1024, start=None,
                           end=None, duration=H, note=None, ip="10.0.0.3", now=T0,
                           auto_advance=True, cards=CARDS).booking
    assert waiting.start == T0 + timedelta(hours=4)
    now = T0 + timedelta(minutes=5)
    _important(store, now=now, gib=79, watching=lambda i: i == victim.id, hours=1)
    # the victim ends at `now`, so the important hour runs now..+1h and the waiting slot
    # moves up behind it instead of waiting out the cancelled three hours.
    assert store.get(waiting.id).start == now + H
