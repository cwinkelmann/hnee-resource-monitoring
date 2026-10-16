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
