"""The server choosing a start: POST without one never fails for lack of capacity."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)
CARD = 81559


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _assign(store, **over):
    d = dict(user="cwinkelmann", gpu=4, vram_mib=20 * 1024, start=None, end=None,
             duration=H, note=None, ip="10.0.0.1", now=T0)
    d.update(over)
    return store.create(**d)


def test_an_empty_card_is_assigned_now(store):
    made = _assign(store)
    assert made.booking.start == T0
    assert made.booking.end == T0 + H
    assert made.assigned_later is False


def test_a_full_card_is_assigned_the_first_free_instant(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    made = _assign(store)
    assert made.booking.start == T0 + timedelta(hours=3)
    assert made.assigned_later is True


def test_an_assigned_slot_is_an_ordinary_row_that_blocks_the_next_one(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    first = _assign(store, vram_mib=79 * 1024)
    second = _assign(store, vram_mib=79 * 1024)
    assert second.booking.start == first.booking.end


def test_gpu_none_floats_to_a_free_card(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    made = _assign(store, gpu=None)
    assert made.booking.start == T0
    assert made.booking.gpu == 0


def test_auto_advance_is_stored(store):
    made = _assign(store, auto_advance=True)
    assert store.get(made.booking.id).auto_advance is True


def test_an_explicit_start_still_conflicts_and_the_409_carries_the_offer(store):
    book(store, gpu=4, gib=79, start=T0, hours=3)
    with pytest.raises(ClaimError) as e:
        _assign(store, start=T0, end=T0 + H, duration=None, vram_mib=20 * 1024)
    assert e.value.status == 409
    assert e.value.extra["earliest"] == (T0 + timedelta(hours=3)).isoformat()
    assert e.value.extra["free_now"] == CARD - 79 * 1024


def test_an_explicit_start_accepts_a_duration_instead_of_an_end(store):
    made = _assign(store, start=T0 + H)
    assert made.booking.start == T0 + H
    assert made.booking.end == T0 + 2 * H
    assert made.assigned_later is False


def test_not_later_than_fails_fast_instead_of_sleeping_for_a_fortnight(store):
    book(store, gpu=4, gib=79, start=T0, hours=10)
    with pytest.raises(ClaimError) as e:
        _assign(store, not_later_than=T0 + timedelta(hours=2))
    assert e.value.status == 409
    assert e.value.extra["earliest"] == (T0 + timedelta(hours=10)).isoformat()


def test_more_than_the_card_is_a_400_not_an_endless_wait(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, vram_mib=90 * 1024)
    assert e.value.status == 400


def test_exactly_one_of_end_and_duration(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, end=T0 + H, duration=H)
    assert e.value.status == 400
    with pytest.raises(ClaimError):
        _assign(store, end=None, duration=None)


def test_nothing_fits_before_the_horizon(store):
    # A booking may last 14 days at most, so the wall is two of them back to back: the
    # second one's end is past HORIZON, so it is never even a candidate instant.
    book(store, gpu=4, gib=79, start=T0, hours=24 * 14)
    book(store, gpu=4, gib=79, start=T0 + timedelta(days=14), hours=24 * 14)
    with pytest.raises(ClaimError) as e:
        _assign(store)
    assert e.value.status == 409
    assert e.value.extra["earliest"] is None
    assert e.value.extra["free_now"] == CARD - 79 * 1024


def test_four_cards_are_assigned_one_shared_start(store):
    made = _assign(store, gpu=None, count=4)
    starts = {b.start for b in made.bookings}
    assert len(made.bookings) == 4
    assert starts == {T0}
    assert sorted(b.gpu for b in made.bookings) == [0, 1, 2, 3]


def test_a_group_shares_one_group_id_and_singles_have_none(store):
    group = _assign(store, gpu=None, count=4)
    ids = {b.group_id for b in group.bookings}
    assert len(ids) == 1 and ids != {None}
    single = _assign(store, gpu=None, count=1)
    assert single.booking.group_id is None


def test_a_group_waits_for_the_last_card(store):
    # A four-card box, each card busy for a different length of time: the group starts when
    # the last one frees, all four together, rather than taking each card as it comes.
    for g in range(4):
        book(store, gpu=g, gib=79, start=T0, hours=g + 1)
    made = _assign(store, gpu=None, count=4, cards={g: CARD for g in range(4)})
    assert {b.start for b in made.bookings} == {T0 + timedelta(hours=4)}
    assert made.assigned_later is True


def test_a_group_larger_than_the_box_is_a_400(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=None, count=9)
    assert e.value.status == 400


def test_count_must_be_a_whole_number_above_zero(store):
    for bad in (0, -1, 1.5, True, "4"):
        with pytest.raises(ClaimError) as e:
            _assign(store, gpu=None, count=bad)
        assert e.value.status == 400


def test_a_group_cannot_name_one_card_or_one_start(store):
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=4, count=2)
    assert e.value.status == 400
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=None, count=2, start=T0, end=T0 + H, duration=None)
    assert e.value.status == 400


def test_every_group_member_is_an_ordinary_row_the_next_booking_respects(store):
    _assign(store, gpu=None, count=8, vram_mib=79 * 1024)
    later = _assign(store, gpu=None, vram_mib=79 * 1024)
    assert later.booking.start == T0 + H


def test_a_user_may_not_hold_more_than_max_pending_future_rows(store):
    # Somebody else holds the card, so every booking below is in the future and counts.
    book(store, gpu=4, gib=79, start=T0, hours=4)
    for _ in range(3):
        _assign(store, max_pending=3, vram_mib=79 * 1024)
    with pytest.raises(ClaimError) as e:
        _assign(store, max_pending=3, vram_mib=79 * 1024)
    assert e.value.status == 409
    assert "3" in e.value.detail                 # the message quotes the real limit
    assert e.value.extra["max_pending_per_user"] == 3
    assert e.value.extra["pending"] == 3


def test_a_started_booking_does_not_count_against_the_limit(store):
    running = _assign(store, max_pending=1, vram_mib=79 * 1024)
    assert running.booking.start == T0           # starts now, so it is not pending
    _assign(store, max_pending=1, vram_mib=79 * 1024)      # the first future one is fine


def test_the_limit_is_per_user(store):
    _assign(store, max_pending=1, vram_mib=79 * 1024)
    _assign(store, max_pending=1, vram_mib=79 * 1024)
    _assign(store, user="duser", max_pending=1, vram_mib=79 * 1024)


def test_a_group_counts_as_one_row_per_card(store):
    with pytest.raises(ClaimError) as e:
        store.create(user="cwinkelmann", gpu=None, vram_mib=79 * 1024, start=None,
                     end=None, duration=H, note=None, ip="10.0.0.1", now=T0, count=4,
                     max_pending=3, cards={g: CARD for g in range(8)})
    assert e.value.status == 409


def test_an_explicit_window_with_no_gpu_picks_a_card(store):
    """`gpu` omitted means "any card" on the assigned path, so it must mean the same
    with an explicit window -- it used to reach the INSERT and answer 500."""
    book(store, gpu=0, gib=79, start=T0, hours=4)
    made = _assign(store, gpu=None, start=T0, end=T0 + H, duration=None,
                   vram_mib=79 * 1024)
    assert made.booking.gpu == 1
    assert made.booking.start == T0


def test_an_explicit_window_with_no_gpu_and_no_room_is_409(store):
    for g in range(8):
        book(store, gpu=g, gib=79, start=T0, hours=4)
    with pytest.raises(ClaimError) as e:
        _assign(store, gpu=None, start=T0, end=T0 + H, duration=None,
                vram_mib=79 * 1024)
    assert e.value.status == 409
