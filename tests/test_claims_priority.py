"""Lendable vs important bookings: the allocation rule, the creation rules, the grace
period, the v2 -> v3 migration and the taken segments."""
import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from resourcemonitor.claims import (Booking, ClaimError, ClaimsStore, allocate, load_active,
                                    taken_segments, takes_of)
from tests.claims_fixture import T0, FakeUsers

GIB = 1024
CARD = 80 * GIB
H = timedelta(hours=1)
CASES = json.loads((Path(__file__).parent / "fixtures/allocate_cases.json").read_text())["cases"]


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def mk(store, user="dorian.zwanzig", gib=40, start=T0, hours=4, now=T0, priority="lendable",
       gpu=4, grace=timedelta(minutes=30)):
    return store.create(user=user, gpu=gpu, vram_mib=gib * GIB, start=start,
                        end=start + hours * H, note=None, ip="10.0.0.1", now=now,
                        card_mib=CARD, priority=priority, grace=grace)


def _b(id, gib, priority, start=T0, end=T0 + 4 * H, user="u"):
    return Booking(id=id, user=user, gpu=4, vram_mib=gib * GIB, start=start, end=end, note=None,
                   created_at=T0, created_ip="x", priority=priority)


# --- allocate --------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_allocate_shared_cases(case):
    bookings = [Booking(id=b["id"], user="u", gpu=0, vram_mib=b["vram_mib"], start=T0,
                        end=T0 + H, note=None, created_at=T0, created_ip="x",
                        priority=b["priority"]) for b in case["bookings"]]
    got = allocate(bookings, case["total"])
    assert {str(k): v for k, v in got.items()} == case["expect"]


# --- creation rules --------------------------------------------------------------------

def test_new_bookings_default_to_lendable(store):
    c = store.create(user="cwinkelmann", gpu=4, vram_mib=GIB, start=T0, end=T0 + H, note=None,
                     ip="x", now=T0, card_mib=CARD)
    assert c.booking.priority == "lendable" and c.takes == [] and c.adjusted_start is None
    assert store.get(c.booking.id).priority == "lendable"


def test_bad_priority_is_400(store):
    with pytest.raises(ClaimError) as e:
        mk(store, priority="urgent")
    assert (e.value.status, e.value.detail) == (400, "priority must be lendable or important")


def test_lendable_must_fit_unbooked(store):
    mk(store, gib=60)
    with pytest.raises(ClaimError) as e:
        mk(store, user="cwinkelmann", gib=30)
    assert e.value.status == 409 and "20.0 GiB unbooked" in e.value.detail


def test_lendable_cannot_use_capacity_squeezed_by_important(store):
    mk(store, gib=60, start=T0 + 2 * H)
    mk(store, user="cwinkelmann", gib=60, priority="important", start=T0 + 2 * H)
    with pytest.raises(ClaimError):
        mk(store, user="andre.kliem", gib=5, start=T0 + 2 * H)


def test_important_ignores_lendable_and_reports_takes(store):
    a = mk(store, gib=60, start=T0 + 2 * H).booking
    c = mk(store, user="cwinkelmann", gib=40, priority="important", start=T0 + 3 * H, hours=2)
    assert c.adjusted_start is None
    assert [(t["id"], t["user"], t["vram_mib"], t["start"], t["end"]) for t in c.takes] == [
        (a.id, "dorian.zwanzig", 20 * GIB, T0 + 3 * H, T0 + 5 * H)]


def test_important_must_fit_with_other_important(store):
    mk(store, gib=50, priority="important", start=T0 + H)
    with pytest.raises(ClaimError) as e:
        mk(store, user="cwinkelmann", gib=40, priority="important", start=T0 + H)
    assert e.value.status == 409
    assert "30.0 GiB not booked as important" in e.value.detail


def test_quick_hold_is_lendable_and_can_be_taken(store):
    q = store.quick(user="dorian.zwanzig", gpu=4, ip="x", now=T0, card_mib=CARD)
    assert q.priority == "lendable"
    c = mk(store, user="cwinkelmann", gib=20, priority="important", start=T0)
    assert c.adjusted_start == T0 and c.booking.start == T0 + timedelta(minutes=30)
    assert [t["id"] for t in c.takes] == [q.id]


def test_lendable_never_takes(store):
    mk(store, gib=20, priority="important")
    c = mk(store, user="cwinkelmann", gib=60)
    assert c.takes == []


# --- grace period ----------------------------------------------------------------------

def test_grace_moves_start_when_squeezing_a_running_lendable(store):
    mk(store, gib=80, start=T0 - H, hours=6, now=T0 - H)
    now = T0 + timedelta(minutes=10)
    c = mk(store, user="cwinkelmann", gib=40, priority="important", start=now, now=now)
    assert c.adjusted_start == now
    assert c.booking.start == now + timedelta(minutes=30)
    assert store.get(c.booking.id).start == now + timedelta(minutes=30)
    assert c.takes[0]["start"] == now + timedelta(minutes=30)


def test_grace_not_applied_to_future_lendable(store):
    mk(store, gib=80, start=T0 + H)
    c = mk(store, user="cwinkelmann", gib=40, priority="important", start=T0 + H)
    assert c.adjusted_start is None and c.booking.start == T0 + H


def test_grace_not_applied_when_nothing_is_squeezed(store):
    mk(store, gib=20, start=T0 - H, hours=6, now=T0 - H)
    c = mk(store, user="cwinkelmann", gib=40, priority="important")
    assert c.adjusted_start is None and c.takes == []


def test_grace_boundary_is_exact(store):
    """A take that already starts exactly at now + G is not moved."""
    mk(store, gib=80, start=T0 - H, hours=6, now=T0 - H)
    start = T0 + timedelta(minutes=30)
    c = mk(store, user="cwinkelmann", gib=40, priority="important", start=start)
    assert c.adjusted_start is None and c.booking.start == start


def test_grace_with_too_short_window_is_409(store):
    mk(store, gib=80, start=T0 - H, hours=6, now=T0 - H)
    with pytest.raises(ClaimError) as e:
        store.create(user="cwinkelmann", gpu=4, vram_mib=40 * GIB, start=T0,
                     end=T0 + timedelta(minutes=20), note=None, ip="x", now=T0, card_mib=CARD,
                     priority="important")
    assert e.value.status == 409 and "30 minutes" in e.value.detail


def test_cancelling_important_gives_capacity_back(store):
    mk(store, gib=80, start=T0 + H)
    imp = mk(store, user="cwinkelmann", gib=40, priority="important", start=T0 + H).booking
    with pytest.raises(ClaimError):
        mk(store, user="andre.kliem", gib=1, start=T0 + H)
    store.cancel(imp.id, ip="x", now=T0)
    bookings = store.list_window(T0)
    lend = next(b for b in bookings if b.user == "dorian.zwanzig")
    assert taken_segments(lend, bookings, CARD, T0, T0 + 10 * H) == []


# --- taken segments --------------------------------------------------------------------

def test_taken_segments_merge_and_name_takers():
    lend = _b(1, 80, "lendable", T0, T0 + 6 * H, user="alice")
    i1 = _b(2, 40, "important", T0 + H, T0 + 3 * H, user="bob")
    i2 = _b(3, 40, "important", T0 + 3 * H, T0 + 4 * H, user="bob")
    i3 = _b(4, 20, "important", T0 + 3 * H, T0 + 5 * H, user="carol")
    segs = taken_segments(lend, [lend, i1, i2, i3], CARD, T0 - H, T0 + 9 * H)
    assert segs == [
        {"start": T0 + H, "end": T0 + 3 * H, "vram_mib": 40 * GIB, "by": ["bob"]},
        {"start": T0 + 3 * H, "end": T0 + 4 * H, "vram_mib": 60 * GIB, "by": ["bob", "carol"]},
        {"start": T0 + 4 * H, "end": T0 + 5 * H, "vram_mib": 20 * GIB, "by": ["carol"]},
    ]


def test_taken_segments_clip_to_window_and_skip_important():
    lend = _b(1, 80, "lendable", T0, T0 + 6 * H)
    imp = _b(2, 40, "important", T0, T0 + 6 * H, user="bob")
    assert taken_segments(lend, [lend, imp], CARD, T0 + H, T0 + 2 * H) == [
        {"start": T0 + H, "end": T0 + 2 * H, "vram_mib": 40 * GIB, "by": ["bob"]}]
    assert taken_segments(imp, [lend, imp], CARD, T0, T0 + 6 * H) == []


def test_takes_of_attributes_only_the_new_booking():
    old = _b(1, 80, "lendable", T0, T0 + 6 * H, user="alice")
    i1 = _b(2, 40, "important", T0, T0 + 6 * H, user="bob")
    new = _b(3, 20, "important", T0 + H, T0 + 2 * H, user="carol")
    assert takes_of(new, [old, i1], CARD) == [
        {"id": 1, "user": "alice", "gpu": 4, "vram_mib": 20 * GIB,
         "start": T0 + H, "end": T0 + 2 * H}]


# --- concurrency -----------------------------------------------------------------------

def test_two_important_bookings_race_for_the_last_share(tmp_path):
    path = tmp_path / "claims.sqlite"
    ClaimsStore(path, users=FakeUsers()).close()
    results, barrier = [], threading.Barrier(2)

    def go(user):
        s = ClaimsStore(path, users=FakeUsers())
        try:
            barrier.wait()
            mk(s, user=user, gib=50, priority="important", start=T0 + H)
            results.append("ok")
        except ClaimError as e:
            results.append(e.status)
        finally:
            s.close()

    ts = [threading.Thread(target=go, args=(u,)) for u in ("cwinkelmann", "andre.kliem")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results, key=str) == [409, "ok"]


# --- migration -------------------------------------------------------------------------

def _v2_file(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE claims (id INTEGER PRIMARY KEY, user TEXT NOT NULL, gpu INTEGER NOT NULL,
          vram_mib INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, note TEXT,
          created_at TEXT NOT NULL, created_ip TEXT NOT NULL, cancelled_at TEXT,
          cancelled_ip TEXT, kind TEXT NOT NULL DEFAULT 'calendar');
        PRAGMA user_version = 2;""")
    conn.execute("INSERT INTO claims (user, gpu, vram_mib, start, end, created_at, created_ip) "
                 "VALUES ('dorian.zwanzig', 4, 40960, ?, ?, ?, '10.0.0.1')",
                 (T0.isoformat(), (T0 + H).isoformat(), T0.isoformat()))
    conn.commit()
    conn.close()


def test_v2_rows_read_as_important_before_and_after_migration(tmp_path):
    path = tmp_path / "claims.sqlite"
    _v2_file(path)
    assert [b.priority for b in load_active(path, T0)] == ["important"]   # unmigrated, mode=ro
    s = ClaimsStore(path, users=FakeUsers())
    try:
        assert [b.priority for b in s.list_window(T0)] == ["important"]
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 3
        c = mk(s, user="cwinkelmann", gib=10, start=T0)
        assert c.booking.priority == "lendable"
    finally:
        s.close()
    ClaimsStore(path, users=FakeUsers()).close()                          # idempotent
    assert sorted(b.priority for b in load_active(path, T0)) == ["important", "lendable"]


def test_to_json_carries_priority():
    assert _b(1, 1, "important").to_json()["priority"] == "important"
