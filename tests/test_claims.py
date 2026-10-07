import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from resourcemonitor.claims import (ClaimError, ClaimsStore, list_window_ro, load_active,
                                     next_reset, parse_time)
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


def test_user_name_with_trailing_newline_is_invalid(store):
    assert _err(lambda: book(store, user="bob\n")) == (400, "invalid user name")


@pytest.mark.parametrize("note", ["del\x7f", "nel\x85"])
def test_del_and_c1_controls_in_note_are_invalid(store, note):
    assert _err(lambda: book(store, note=note)) == (400, "invalid note")


def test_conflict_end_is_capped_at_the_requested_end(store):
    book(store, user="cwinkelmann", gib=79, hours=4)
    status, detail = _err(lambda: book(store, gib=10, hours=1))
    assert status == 409 and "between Tue 06 Oct 12:00 UTC and Tue 06 Oct 13:00 UTC" in detail


def test_load_active_fails_open_on_a_malformed_row(store, tmp_path, capsys):
    book(store)
    conn = sqlite3.connect(tmp_path / "c.sqlite")
    with conn:
        conn.execute("INSERT INTO claims (user, gpu, vram_mib, start, end, created_at, created_ip) "
                     "VALUES ('x', 4, 1024, '2026-10-06 garbage', '9999-12-31', 'garbage', 'ip')")
    conn.close()
    assert load_active(tmp_path / "c.sqlite", T0 + timedelta(hours=1)) == []
    assert "claims unavailable: ValueError" in capsys.readouterr().out


def test_list_window_ro_matches_the_store_and_is_fail_open(store, tmp_path, capsys):
    a = book(store)
    c = book(store, gpu=5, start=T0 + timedelta(days=3))
    store.cancel(c.id, ip="10.0.0.1", now=T0)
    far = book(store, gpu=6, start=T0 + timedelta(days=10))
    path = tmp_path / "c.sqlite"
    assert [b.id for b in list_window_ro(path, T0, days_ahead=14, days_back=7)] == \
        [b.id for b in store.list_window(T0, days_ahead=14, days_back=7)]
    assert [b.id for b in list_window_ro(path, T0, days_ahead=1, days_back=7)] == [a.id]
    assert far.id in [b.id for b in list_window_ro(path, T0, 14, 7)]
    assert list_window_ro(tmp_path / "missing.sqlite", T0, 14, 7) == []
    assert capsys.readouterr().out == ""


def test_readers_fail_open_on_any_exception_class(store, tmp_path, capsys, monkeypatch):
    book(store)
    path = tmp_path / "c.sqlite"
    def boom(r):
        raise KeyError("x")
    monkeypatch.setattr("resourcemonitor.claims._row", boom)
    assert load_active(path, T0 + timedelta(hours=1)) == []
    assert list_window_ro(path, T0, 14, 7) == []
    assert capsys.readouterr().out.count("claims unavailable: KeyError") == 2


# ---------- quick booking (Task 8) ----------
UTC = timezone.utc


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 6, 6, 59, tzinfo=UTC), datetime(2026, 10, 6, 7, 0, tzinfo=UTC)),   # 08:59 CEST
    (datetime(2026, 10, 6, 7, 0, tzinfo=UTC), datetime(2026, 10, 7, 7, 0, tzinfo=UTC)),    # 09:00 exactly
    (datetime(2026, 10, 6, 21, 0, tzinfo=UTC), datetime(2026, 10, 7, 7, 0, tzinfo=UTC)),   # 23:00 CEST
    (datetime(2026, 10, 24, 20, 0, tzinfo=UTC), datetime(2026, 10, 25, 8, 0, tzinfo=UTC)),  # CEST -> CET
    (datetime(2026, 3, 28, 22, 0, tzinfo=UTC), datetime(2026, 3, 29, 7, 0, tzinfo=UTC)),   # CET -> CEST
])
def test_next_reset_is_the_next_0900_berlin_strictly_after_now(now, expected):
    r = next_reset(now)
    assert r == expected and r.utcoffset() == timedelta(0)


TOMORROW_9 = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)          # T0 is 14:00 Berlin


def test_quick_books_the_whole_card_until_0900(store):
    b = store.quick(user="dorian.zwanzig", gpu=3, ip="10.0.0.1", now=T0, card_mib=81559)
    assert (b.user, b.gpu, b.vram_mib, b.kind, b.note) == ("dorian.zwanzig", 3, 81559, "quick", "quick booking")
    assert b.start == T0 and b.end == TOMORROW_9
    assert store.get(b.id) == b and b.to_json()["kind"] == "quick"


def test_quick_replace_cancels_the_old_one_with_the_ip(store):
    old = store.quick(user="dorian.zwanzig", gpu=3, ip="10.0.0.1", now=T0)
    later = T0 + timedelta(minutes=10)
    new = store.quick(user="andre.kliem", gpu=3, ip="10.0.0.2", now=later)
    gone = store.get(old.id)
    assert (gone.cancelled_at, gone.cancelled_ip) == (later, "10.0.0.2")
    assert new.user == "andre.kliem" and new.cancelled_at is None
    assert [b.id for b in load_active(store_path(store), later)] == [new.id]


def store_path(store):
    return store._conn.execute("PRAGMA database_list").fetchone()[2]


def test_quick_release_and_release_when_none(store):
    b = store.quick(user="dorian.zwanzig", gpu=3, ip="10.0.0.1", now=T0)
    assert store.quick(user=None, gpu=3, ip="10.0.0.3", now=T0 + timedelta(minutes=1)) is None
    assert store.get(b.id).cancelled_ip == "10.0.0.3"
    assert store.quick(user=None, gpu=3, ip="10.0.0.3", now=T0 + timedelta(minutes=2)) is None


def test_quick_leaves_other_gpus_alone(store):
    other = store.quick(user="dorian.zwanzig", gpu=2, ip="10.0.0.1", now=T0)
    store.quick(user=None, gpu=3, ip="10.0.0.1", now=T0)
    assert store.get(other.id).cancelled_at is None


@pytest.mark.parametrize("kwargs, detail", [
    ({"gpu": 8}, "GPU must be 0–7"),
    ({"gpu": -1}, "GPU must be 0–7"),
    ({"user": "Bad Name"}, "invalid user name"),
    ({"user": "nobody.here"}, "unknown user 'nobody.here'"),
])
def test_quick_validation(store, kwargs, detail):
    args = {"user": "dorian.zwanzig", "gpu": 3, "ip": "10.0.0.1", "now": T0, **kwargs}
    assert _err(lambda: store.quick(**args)) == (400, detail)


def test_quick_is_refused_while_a_calendar_booking_is_active(store):
    cal = book(store, gpu=3, gib=10)
    status, detail = _err(lambda: store.quick(user="andre.kliem", gpu=3, ip="x", now=T0))
    assert (status, detail) == (409, "GPU 3 has calendar bookings — use the calendar")
    assert _err(lambda: store.quick(user=None, gpu=3, ip="x", now=T0))[0] == 409
    assert store.get(cal.id).cancelled_at is None


def test_quick_end_is_clipped_to_a_future_calendar_booking(store):
    book(store, gpu=3, gib=10, start=T0 + timedelta(hours=6), now=T0)
    b = store.quick(user="andre.kliem", gpu=3, ip="x", now=T0)
    assert b.end == T0 + timedelta(hours=6)
    # The calendar booking is never overbooked: it starts exactly where the quick one ends.


def test_quick_is_refused_when_less_than_a_minute_remains(store):
    book(store, gpu=3, gib=10, start=T0 + timedelta(seconds=59), hours=1, now=T0)
    status, detail = _err(lambda: store.quick(user="andre.kliem", gpu=3, ip="x", now=T0))
    assert (status, detail) == (409, "GPU 3 is booked from Tue 06 Oct 12:00 UTC — use the calendar")


def test_quick_with_exactly_one_minute_is_allowed(store):
    book(store, gpu=3, gib=10, start=T0 + timedelta(minutes=1), hours=1, now=T0)
    assert store.quick(user="andre.kliem", gpu=3, ip="x", now=T0).end == T0 + timedelta(minutes=1)


def test_create_still_makes_calendar_bookings(store):
    assert book(store).kind == "calendar" and book(store, gpu=5).to_json()["kind"] == "calendar"


_V1_SCHEMA = """
CREATE TABLE claims (
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, gpu INTEGER NOT NULL CHECK (gpu BETWEEN 0 AND 7),
  vram_mib INTEGER NOT NULL CHECK (vram_mib > 0), start TEXT NOT NULL, end TEXT NOT NULL,
  note TEXT, created_at TEXT NOT NULL, created_ip TEXT NOT NULL, cancelled_at TEXT,
  cancelled_ip TEXT);
CREATE INDEX claims_gpu_window ON claims(gpu, start, end);
INSERT INTO claims (user, gpu, vram_mib, start, end, note, created_at, created_ip)
  VALUES ('dorian.zwanzig', 4, 40960, '2026-10-06T12:00:00+00:00', '2026-10-06T16:00:00+00:00',
          NULL, '2026-10-06T12:00:00+00:00', '10.0.0.1');
PRAGMA user_version = 1;
"""


def _v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V1_SCHEMA)
    conn.close()


def test_version_1_file_reads_as_calendar_before_migration(tmp_path):
    path = tmp_path / "v1.sqlite"
    _v1(path)
    (a,) = load_active(path, T0 + timedelta(hours=1))
    (w,) = list_window_ro(path, T0, 14, 7)
    assert a.kind == w.kind == "calendar"
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1   # readers never migrate
    conn.close()


def test_opening_a_version_1_file_migrates_it_to_the_current_version(tmp_path):
    path = tmp_path / "v1.sqlite"
    _v1(path)
    s = ClaimsStore(path, users=FakeUsers())
    try:
        assert s.get(1).kind == "calendar" and s.get(1).user == "dorian.zwanzig"
        assert [b.kind for b in s.list_window(T0)] == ["calendar"]
        assert s.get(1).priority == "important"
    finally:
        s.close()
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)")]
    assert {"kind", "priority"} <= set(cols)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()
    ClaimsStore(path, users=FakeUsers()).close()                    # reopening is a no-op


def test_new_files_are_created_at_the_current_version_with_kind(tmp_path):
    ClaimsStore(tmp_path / "n.sqlite", users=FakeUsers()).close()
    conn = sqlite3.connect(tmp_path / "n.sqlite")
    assert "kind" in [r[1] for r in conn.execute("PRAGMA table_info(claims)")]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()


def test_calendar_booking_on_a_quick_held_card_names_the_holder(store):
    store.quick(user="dorian.zwanzig", gpu=3, ip="x", now=T0)
    status, detail = _err(lambda: book(store, user="andre.kliem", gpu=3, gib=10,
                                       start=T0 + timedelta(hours=1)))
    assert (status, detail) == (409, "GPU 3 is held by dorian.zwanzig until 09:00 "
                                     "(quick booking) — set its holder to free first")


def test_quick_hold_conflict_shows_the_clipped_end_in_berlin_time(store):
    book(store, gpu=3, gib=10, start=T0 + timedelta(hours=2), hours=1, now=T0)   # 16:00 Berlin
    store.quick(user="dorian.zwanzig", gpu=3, ip="x", now=T0)
    status, detail = _err(lambda: book(store, user="andre.kliem", gpu=3, gib=10, hours=1))
    assert (status, detail) == (409, "GPU 3 is held by dorian.zwanzig until 16:00 "
                                     "(quick booking) — set its holder to free first")
