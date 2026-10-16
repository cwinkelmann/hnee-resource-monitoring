"""Pure capacity arithmetic: no DB, no clock, no GPU."""
from datetime import datetime, timedelta, timezone

from resourcemonitor.claims import Booking
from resourcemonitor.slots import HORIZON, booked_at, earliest_fit, fits

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CARD = 81559
H = timedelta(hours=1)


def b(gpu=0, gib=20, start_h=0, hours=1, bid=1, cancelled=False):
    start = T0 + timedelta(hours=start_h)
    return Booking(id=bid, user="alice", gpu=gpu, vram_mib=gib * 1024, start=start,
                   end=start + timedelta(hours=hours), note=None, created_at=T0,
                   created_ip="10.0.0.1",
                   cancelled_at=T0 if cancelled else None)


def test_booked_at_sums_only_live_overlapping_rows_on_that_card():
    rows = [b(bid=1), b(bid=2), b(gpu=1, bid=3), b(bid=4, cancelled=True),
            b(bid=5, start_h=5)]
    assert booked_at(rows, 0, T0) == 40 * 1024
    assert booked_at(rows, 1, T0) == 20 * 1024
    assert booked_at(rows, 0, T0 + timedelta(hours=2)) == 0


def test_empty_card_fits_now():
    assert earliest_fit([], 0, 20 * 1024, H, CARD, T0) == T0


def test_a_full_card_fits_when_the_blocking_booking_ends():
    rows = [b(gib=79, hours=3)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=3)


def test_the_whole_window_must_fit_not_just_its_start():
    # free now, but a 79 GiB booking starts in 30 minutes: a 1 h slot cannot start now.
    rows = [b(gib=79, start_h=0.5, hours=2)]
    got = earliest_fit(rows, 0, 20 * 1024, H, CARD, T0)
    assert got == T0 + timedelta(hours=2, minutes=30)


def test_one_mib_too_much_waits_and_an_exact_fit_does_not():
    rows = [b(gib=61, hours=2)]           # 61 + 20 GiB = 82944 MiB, past the 81559 card
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=2)
    rows = [b(gib=59, hours=2)]           # 59 + 20 GiB = 80896 MiB, fits beside it now
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0


def test_a_gap_between_two_bookings_is_found():
    rows = [b(gib=79, hours=1, bid=1), b(gib=79, start_h=3, hours=5, bid=2)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=1)


def test_a_gap_too_short_for_the_duration_is_skipped():
    rows = [b(gib=79, hours=1, bid=1), b(gib=79, start_h=1.5, hours=5, bid=2)]
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) == T0 + timedelta(hours=6, minutes=30)


def test_cancelled_rows_never_block():
    assert earliest_fit([b(gib=79, hours=9, cancelled=True)], 0, 20 * 1024, H, CARD, T0) == T0


def test_more_than_the_card_never_fits():
    assert earliest_fit([], 0, 90 * 1024, H, CARD, T0) is None


def test_nothing_beyond_the_horizon_is_offered():
    rows = [b(gib=79, hours=24 * 20)]     # 20 days
    assert earliest_fit(rows, 0, 20 * 1024, H, CARD, T0) is None


def test_fits_is_exclusive_at_the_end_so_back_to_back_slots_work():
    rows = [b(gib=79, hours=1)]
    assert fits(rows, 0, 20 * 1024, T0 + H, H, CARD) is True
    assert fits(rows, 0, 20 * 1024, T0, H, CARD) is False


def test_horizon_matches_the_fourteen_day_booking_limit():
    assert HORIZON == timedelta(days=14)
