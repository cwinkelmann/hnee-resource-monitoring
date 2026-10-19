"""The in-memory registry behind GET /api/claims/<id>/events. Threads, no HTTP, no DB."""
import threading
import time

from resourcemonitor.watchers import Watchers


def test_nothing_is_attached_to_begin_with():
    w = Watchers()
    assert w.attached(1) is False
    assert w.count() == 0


def test_registering_attaches_and_closing_detaches():
    w = Watchers()
    watch = w.register(7)
    assert watch is not None
    assert w.attached(7) is True and w.count() == 1
    watch.close()
    assert w.attached(7) is False and w.count() == 0


def test_the_context_manager_detaches_even_on_an_exception():
    w = Watchers()
    try:
        with w.register(7):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert w.attached(7) is False


def test_a_waiter_is_woken_with_the_event():
    w = Watchers()
    got = []
    watch = w.register(7)

    def reader():
        got.append(watch.wait(timeout=5))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    assert w.notify(7, {"event": "taken"}) == 1
    t.join(5)
    assert got == [{"event": "taken"}]


def test_an_event_that_arrives_before_the_wait_is_not_lost():
    w = Watchers()
    watch = w.register(7)
    w.notify(7, {"event": "taken"})
    assert watch.wait(timeout=0.1) == {"event": "taken"}


def test_waiting_times_out_with_none():
    w = Watchers()
    watch = w.register(7)
    assert watch.wait(timeout=0.05) is None


def test_notifying_a_booking_nobody_watches_is_harmless():
    assert Watchers().notify(7, {"event": "taken"}) == 0


def test_two_watchers_on_one_booking_are_both_woken():
    w = Watchers()
    a, b = w.register(7), w.register(7)
    assert w.notify(7, {"event": "taken"}) == 2
    assert a.wait(timeout=0.1) == b.wait(timeout=0.1) == {"event": "taken"}


def test_the_cap_refuses_the_next_registration():
    w = Watchers(max_watchers=2)
    assert w.register(1) is not None
    assert w.register(2) is not None
    assert w.register(3) is None
    assert w.count() == 2


def test_a_closed_watch_frees_a_slot():
    w = Watchers(max_watchers=1)
    first = w.register(1)
    assert w.register(2) is None
    first.close()
    assert w.register(2) is not None


def test_shutdown_wakes_everyone_and_refuses_new_registrations():
    w = Watchers()
    watch = w.register(7)
    done = threading.Event()

    def reader():
        assert watch.wait(timeout=5) is None
        done.set()

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    w.shutdown()
    assert done.wait(5) is True
    assert w.register(8) is None
    t.join(5)
