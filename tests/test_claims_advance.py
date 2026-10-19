"""A slot moves earlier only inside the transaction of a write that frees capacity."""
from datetime import timedelta

import pytest

from resourcemonitor.claims import GPU_COUNT, ClaimsStore, DEFAULT_CARD_MIB
from tests.claims_fixture import T0, FakeUsers, book

H = timedelta(hours=1)
CARDS = {g: DEFAULT_CARD_MIB for g in range(GPU_COUNT)}
CARDS4 = {g: DEFAULT_CARD_MIB for g in range(4)}


@pytest.fixture
def store(tmp_path):
    s = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
    yield s
    s.close()


def _assign(store, now=T0, **over):
    d = dict(user="cwinkelmann", gpu=4, vram_mib=79 * 1024, start=None, end=None,
             duration=H, note=None, ip="10.0.0.1", now=now, auto_advance=True,
             cards=CARDS)
    d.update(over)
    return store.create(**d).booking


def test_an_early_release_pulls_the_next_slot_earlier(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    assert waiting.start == T0 + timedelta(hours=4)
    out = store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == T0 + H
    assert out.advanced == [{"id": waiting.id, "user": "cwinkelmann", "gpu": 4,
                             "old_start": (T0 + timedelta(hours=4)).isoformat(),
                             "new_start": (T0 + H).isoformat()}]


def test_duration_is_preserved_and_the_id_is_kept(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store, duration=timedelta(hours=2))
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    moved = store.get(waiting.id)
    assert moved.id == waiting.id
    assert moved.end - moved.start == timedelta(hours=2)


def test_a_booking_without_auto_advance_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store, auto_advance=False)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == waiting.start


def test_a_booking_already_under_way_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    running = _assign(store, start=T0, end=T0 + timedelta(hours=9), duration=None,
                      gpu=5, vram_mib=20 * 1024)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(running.id).start == T0


def test_a_cancelled_slot_never_moves(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    store.cancel(waiting.id, ip="10.0.0.1", now=T0, cards=CARDS)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start == waiting.start


def test_a_natural_end_moves_nothing_because_nothing_was_freed(store):
    book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    # time simply passes: no write, so no advance, and the slot is where it was priced.
    assert store.get(waiting.id).start == T0 + timedelta(hours=4)


def test_advances_cascade_against_the_updated_timeline(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    first = _assign(store)
    second = _assign(store)
    assert (first.start, second.start) == (T0 + timedelta(hours=4),
                                           T0 + timedelta(hours=5))
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(first.id).start == T0 + H
    assert store.get(second.id).start == T0 + timedelta(hours=2)


def test_a_cancel_advances_too(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    assert waiting.start == T0 + timedelta(hours=4)
    store.cancel(blocker.id, ip="10.0.0.1", now=T0, cards=CARDS)
    assert store.get(waiting.id).start == T0


def test_a_group_moves_only_as_a_whole(store):
    # A four-card box with all four busy, so the group has to wait and then has somewhere
    # to move to. Releasing one card is not enough: the set moves together or not at all.
    blockers = [book(store, gpu=g, gib=79, start=T0, hours=4) for g in range(4)]
    group = store.create(user="cwinkelmann", gpu=None, vram_mib=79 * 1024, start=None,
                         end=None, duration=H, note=None, ip="10.0.0.1", now=T0,
                         count=4, auto_advance=True, cards=CARDS4).bookings
    assert {b.start for b in group} == {T0 + timedelta(hours=4)}
    store.release(blockers[0].id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS4)
    assert {store.get(b.id).start for b in group} == {T0 + timedelta(hours=4)}
    for blocker in blockers[1:]:
        store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS4)
    assert {store.get(b.id).start for b in group} == {T0 + H}


def test_a_slot_never_advances_into_the_past(store):
    blocker = book(store, gpu=4, gib=79, start=T0, hours=4)
    waiting = _assign(store)
    store.release(blocker.id, end=None, ip="10.0.0.1", now=T0 + H, cards=CARDS)
    assert store.get(waiting.id).start >= T0 + H
