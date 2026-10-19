"""Giving a card back honestly: the window used stays on the calendar."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import ClaimError, ClaimsStore
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def test_a_started_booking_is_truncated_not_cancelled(store):
    b = book(store, start=T0, hours=4)
    out = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=40))
    assert out.booking.end == T0 + timedelta(minutes=40)
    assert out.booking.cancelled_at is None       # it still happened; energy still counts


def test_a_not_yet_started_booking_falls_back_to_cancel(store):
    b = book(store, start=T0 + timedelta(hours=2), hours=4, now=T0)
    out = store.release(b.id, end=None, ip="10.0.0.1", now=T0)
    assert out.booking.cancelled_at == T0
    assert out.booking.end == b.end                # untouched: end < start would be nonsense


def test_truncation_frees_the_tail_for_someone_else(store):
    b = book(store, gpu=4, gib=79, start=T0, hours=4)
    store.release(b.id, end=None, ip="10.0.0.1", now=T0 + H)
    made = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None,
                        end=None, duration=H, note=None, ip="10.0.0.1", now=T0 + H)
    assert made.booking.start == T0 + H


def test_releasing_an_already_ended_booking_is_a_no_op(store):
    b = book(store, start=T0, hours=1)
    first = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=30))
    again = store.release(b.id, end=None, ip="10.0.0.1", now=T0 + timedelta(minutes=50))
    assert again.booking.end == first.booking.end


def test_releasing_a_cancelled_booking_is_a_409(store):
    b = book(store, start=T0, hours=4)
    store.cancel(b.id, ip="10.0.0.1", now=T0)
    with pytest.raises(ClaimError) as e:
        store.release(b.id, end=None, ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 409


def test_an_unknown_booking_is_a_404(store):
    with pytest.raises(ClaimError) as e:
        store.release(9999, end=None, ip="10.0.0.1", now=T0)
    assert e.value.status == 404


def test_an_end_before_the_start_is_a_400(store):
    b = book(store, start=T0, hours=4)
    with pytest.raises(ClaimError) as e:
        store.release(b.id, end=T0 - H, ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 400


def test_extending_rechecks_capacity_and_can_lose_to_an_assigned_slot(store):
    mine = book(store, gpu=4, gib=79, start=T0, hours=2)
    store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None, end=None,
                 duration=H, note=None, ip="10.0.0.1", now=T0)      # lands at T0+2h
    with pytest.raises(ClaimError) as e:
        store.release(mine.id, end=T0 + timedelta(hours=3), ip="10.0.0.1", now=T0 + H)
    assert e.value.status == 409


def test_extending_into_free_space_succeeds(store):
    b = book(store, gpu=4, gib=79, start=T0, hours=2)
    out = store.release(b.id, end=T0 + timedelta(hours=3), ip="10.0.0.1", now=T0 + H)
    assert out.booking.end == T0 + timedelta(hours=3)


def test_cancelling_a_not_yet_started_group_member_cancels_the_set(store):
    made = store.create(user="cwinkelmann", gpu=None, vram_mib=20 * 1024, start=None,
                        end=None, duration=H, note=None, ip="10.0.0.1", now=T0, count=4)
    store.cancel(made.bookings[0].id, ip="10.0.0.1", now=T0 - timedelta(minutes=1))
    assert all(store.get(b.id).cancelled_at is not None for b in made.bookings)


def test_releasing_an_under_way_group_member_truncates_only_that_row(store):
    made = store.create(user="cwinkelmann", gpu=None, vram_mib=20 * 1024, start=None,
                        end=None, duration=timedelta(hours=4), note=None, ip="10.0.0.1",
                        now=T0, count=4)
    store.release(made.bookings[0].id, end=None, ip="10.0.0.1", now=T0 + H)
    assert store.get(made.bookings[0].id).end == T0 + H
    assert store.get(made.bookings[1].id).end == T0 + timedelta(hours=4)


def test_releasing_a_pending_booking_to_a_later_end_never_cancels_it(store):
    """An extension is not a release. Routing it to cancel() destroys the booking the
    caller was trying to lengthen, and answers 200 while doing it."""
    b = book(store, start=T0 + timedelta(hours=2), hours=1, now=T0)
    with pytest.raises(ClaimError) as e:
        store.release(b.id, end=T0 + timedelta(hours=5), ip="10.0.0.1", now=T0)
    assert e.value.status == 409
    assert store.get(b.id).cancelled_at is None


def test_releasing_a_pending_booking_reports_what_it_advanced(store):
    # The card is busy now and booked solid for four hours after that, so the waiting slot
    # sits behind both. Giving the pending one back is what frees the gap.
    book(store, gpu=4, gib=79, start=T0, hours=1)
    blocker = book(store, gpu=4, gib=79, start=T0 + timedelta(hours=1), hours=4, now=T0)
    waiting = store.create(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None,
                           end=None, duration=H, note=None, ip="10.0.0.1", now=T0,
                           auto_advance=True).booking
    assert waiting.start == T0 + timedelta(hours=5)
    out = store.release(blocker.id, end=None, ip="10.0.0.1", now=T0)
    assert out.booking.cancelled_at is not None          # it had not started
    assert [a["id"] for a in out.advanced] == [waiting.id]
