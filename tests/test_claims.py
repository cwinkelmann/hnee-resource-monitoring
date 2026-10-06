import sqlite3
import threading
from datetime import timedelta, timezone

import pytest

from resourcemonitor.claims import (Booking, ClaimError, ClaimsStore, load_active, parse_time)
from tests.claims_fixture import FakeUsers, T0, book


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "c.sqlite", users=FakeUsers())
    yield s
    s.close()


def _err(fn):
    with pytest.raises(ClaimError) as e:
        fn()
    return e.value.status, e.value.detail


def test_create_returns_the_stored_booking(store):
    b = book(store, note="KEV eval")
    assert (b.user, b.gpu, b.vram_mib, b.note, b.created_ip) == ("dorian.zwanzig", 4, 40960, "KEV eval", "10.0.0.1")
    assert b.start == T0 and b.end == T0 + timedelta(hours=4) and b.cancelled_at is None
    assert store.get(b.id) == b


@pytest.mark.parametrize("kwargs, status, detail", [
    ({"user": "Bad Name"}, 400, "invalid user name"),
    ({"user": "dorain.zwanzig"}, 400, "unknown user 'dorain.zwanzig'"),
    ({"gpu": 8}, 400, "GPU must be 0–7"),
    ({"gib": 0}, 400, "VRAM must be between 1 and 79 GiB"),
    ({"gib": 80}, 400, "VRAM must be between 1 and 79 GiB"),
    ({"hours": 0}, 400, "end must be after start"),
    ({"start": T0 - timedelta(minutes=6)}, 400, "start lies in the past"),
    ({"hours": 14 * 24 + 1}, 400, "a booking may last at most 14 days"),
    ({"note": "x" * 121}, 400, "note too long"),
    ({"note": "evil\x07"}, 400, "invalid note"),
])
def test_validation_errors_have_exact_details(store, kwargs, status, detail):
    assert _err(lambda: book(store, **kwargs)) == (status, detail)


def test_start_within_five_minutes_in_the_past_is_accepted(store):
    assert book(store, start=T0 - timedelta(minutes=4)).start == T0 - timedelta(minutes=4)


def test_exactly_fourteen_days_is_allowed(store):
    assert book(store, hours=14 * 24).end == T0 + timedelta(days=14)


def test_blank_note_is_stored_as_null(store):
    assert book(store, note="   ").note is None


def test_overbooking_is_409_with_the_free_amount(store):
    book(store, user="cwinkelmann", gib=40)
    status, detail = _err(lambda: book(store, user="andre.kliem", gib=40))
    assert status == 409
    assert detail.startswith("GPU 4 has only 39.6 GiB unbooked between Tue 06 Oct 12:00 UTC and Tue 06 Oct 16:00 UTC")


def test_two_shares_that_fit_both_succeed(store):
    book(store, user="cwinkelmann", gib=40)
    assert book(store, user="andre.kliem", gib=39).vram_mib == 39 * 1024


def test_back_to_back_full_card_bookings_do_not_conflict(store):
    book(store, gib=79, hours=4)
    assert book(store, user="cwinkelmann", gib=79, start=T0 + timedelta(hours=4)).gpu == 4


def test_capacity_is_checked_at_the_worst_instant(store):
    # A: 30 GiB 12-14, B: 30 GiB 13-16. New 30 GiB 12-16 collides only during 13-14 (90 > 79.6).
    book(store, user="cwinkelmann", gib=30, hours=2)
    book(store, user="andre.kliem", gib=30, start=T0 + timedelta(hours=1), hours=3)
    status, detail = _err(lambda: book(store, gib=30, hours=4))
    assert status == 409 and "between Tue 06 Oct 13:00 UTC and Tue 06 Oct 14:00 UTC" in detail


def test_other_gpus_and_cancelled_bookings_do_not_count(store):
    other = book(store, user="cwinkelmann", gpu=5, gib=79)
    gone = book(store, user="andre.kliem", gib=79)
    store.cancel(gone.id, ip="10.0.0.2", now=T0)
    assert book(store, gib=79).gpu == 4 and other.gpu == 5


def test_cancel_records_audit_and_is_idempotent(store):
    b = book(store)
    c1 = store.cancel(b.id, ip="10.0.0.9", now=T0 + timedelta(minutes=1))
    c2 = store.cancel(b.id, ip="10.0.0.7", now=T0 + timedelta(minutes=2))
    assert c1.cancelled_ip == "10.0.0.9" and c2 == c1          # second cancel changes nothing
    assert _err(lambda: store.cancel(9999, ip="x", now=T0)) == (404, "no such booking")


def test_rows_are_never_deleted(store, tmp_path):
    b = book(store)
    store.cancel(b.id, ip="10.0.0.9", now=T0)
    n = sqlite3.connect(tmp_path / "c.sqlite").execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert n == 1


def test_active_at_is_start_inclusive_end_exclusive(store):
    b = book(store)
    assert b.active_at(T0) and not b.active_at(T0 + timedelta(hours=4))
    assert not b.active_at(T0 - timedelta(seconds=1))


def test_list_window_includes_recent_cancelled_and_upcoming(store):
    past = book(store, start=T0, hours=1)
    upcoming = book(store, user="cwinkelmann", gpu=5, start=T0 + timedelta(days=3))
    store.cancel(past.id, ip="10.0.0.3", now=T0)
    ids = [b.id for b in store.list_window(T0 + timedelta(days=1), days_ahead=14, days_back=7)]
    assert ids == [past.id, upcoming.id]


def test_parallel_bookings_for_the_last_share_exactly_one_wins(tmp_path):
    path = tmp_path / "c.sqlite"
    ClaimsStore(path, users=FakeUsers()).close()
    results = []

    def attempt(user):
        s = ClaimsStore(path, users=FakeUsers())
        try:
            book(s, user=user, gib=60)
            results.append("ok")
        except ClaimError as e:
            results.append(e.status)
        finally:
            s.close()

    ts = [threading.Thread(target=attempt, args=(u,)) for u in ("cwinkelmann", "andre.kliem")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results, key=str) == [409, "ok"]


@pytest.mark.parametrize("text, expected", [
    ("2026-10-25T02:30:00+02:00", "2026-10-25T00:30:00+00:00"),
    ("2026-10-25T02:30:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-06T12:00:00Z", "2026-10-06T12:00:00+00:00"),
])
def test_parse_time_normalises_offsets_to_utc(text, expected):
    assert parse_time(text).isoformat() == expected


@pytest.mark.parametrize("text", ["2026-10-25T02:30", "tomorrow", "", "2026-13-01T00:00+00:00"])
def test_parse_time_rejects_naive_or_garbage(text):
    assert _err(lambda: parse_time(text)) == (400, "times need a date, time and UTC offset")


def test_load_active_is_read_only_and_fail_open(store, tmp_path, capsys):
    b = book(store)
    assert [x.id for x in load_active(tmp_path / "c.sqlite", T0 + timedelta(hours=1))] == [b.id]
    assert load_active(tmp_path / "c.sqlite", T0 + timedelta(hours=5)) == []
    assert load_active(tmp_path / "missing.sqlite", T0) == []
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database at all" * 100)
    assert load_active(bad, T0) == []
    assert "claims unavailable: DatabaseError" in capsys.readouterr().out
