"""The long-poll a running job dials out on. Real HTTP, real threads."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from resourcemonitor import web
from resourcemonitor.web import make_server
from tests.claims_fixture import T0, FakeUsers
from tests.history_fixture import build_history

HOLD = 1.0


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    path = tmp_path_factory.mktemp("hist") / "h.sqlite"
    build_history(path, T0 - timedelta(hours=2), hours=2)
    return path


@pytest.fixture(autouse=True)
def fast_hold(monkeypatch):
    """A 60 s hold is right in production and unusable in a test."""
    monkeypatch.setattr(web, "EVENT_HOLD_S", HOLD)
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter())


@pytest.fixture
def live(tmp_path, history):
    pol = tmp_path / "policy.toml"
    pol.write_text("")
    srv = make_server("127.0.0.1", 0, history, pol, stale_after_s=600, clock=lambda: T0,
                      claims_path=tmp_path / "claims.sqlite", users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _book(base, **over):
    d = {"user": "cwinkelmann", "gpu": 4, "vram_gib": 79, "start": T0.isoformat(),
         "end": (T0 + timedelta(hours=4)).isoformat(), "priority": "lendable",
         "notice_seconds": 0}
    d.update(over)
    return _post(base + "/api/claims", d)[1]["claim"]


def test_an_unknown_booking_is_404_and_parks_nothing(live):
    srv, base = live
    assert _get(base + "/api/claims/999/events")[0] == 404
    assert srv.watchers.count() == 0


def test_a_cancelled_booking_is_409(live):
    srv, base = live
    claim = _book(base)
    _post(f"{base}/api/claims/{claim['id']}/cancel", {})
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 409
    assert srv.watchers.count() == 0


def test_a_booking_already_over_is_409(live):
    srv, base = live
    # A start more than PAST_SLACK in the past is refused outright, so the only way to
    # have a finished booking against a fixed clock is one that began inside the slack.
    claim = _book(base, start=(T0 - timedelta(minutes=4)).isoformat(),
                  end=(T0 - timedelta(minutes=1)).isoformat())
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 409


def test_nothing_happening_answers_204_after_the_hold(live):
    srv, base = live
    claim = _book(base)
    began = time.monotonic()
    status, body = _get(f"{base}/api/claims/{claim['id']}/events")
    assert status == 204 and body is None
    assert HOLD <= time.monotonic() - began < HOLD + 5
    assert srv.watchers.count() == 0


def test_the_watcher_is_woken_when_an_important_booking_takes_the_card(live):
    srv, base = live
    victim = _book(base)
    got = []

    def watch():
        got.append(_get(f"{base}/api/claims/{victim['id']}/events", timeout=20))

    t = threading.Thread(target=watch)
    t.start()
    for _ in range(100):                       # wait for the connection to be parked
        if srv.watchers.attached(victim["id"]):
            break
        time.sleep(0.02)
    assert srv.watchers.attached(victim["id"]) is True
    status, body = _post(base + "/api/claims",
                         {"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 79,
                          "start": T0.isoformat(),
                          "end": (T0 + timedelta(hours=1)).isoformat(),
                          "priority": "important"})
    assert status == 201
    assert [r["id"] for r in body["revoked"]] == [victim["id"]]
    t.join(20)
    assert got[0][0] == 200
    assert got[0][1]["event"] == "taken"
    # 79 GiB taken from a 79 GiB booking leaves a 663 MiB crumb, below anything bookable.
    assert got[0][1]["effective_mib"] < 1024
    assert got[0][1]["truncated"] is True
    assert got[0][1]["by"] == ["dorian.zwanzig"]


def test_past_max_watchers_the_route_answers_503(live, monkeypatch):
    srv, base = live
    claim = _book(base)
    srv.watchers.max_watchers = 0
    assert _get(f"{base}/api/claims/{claim['id']}/events")[0] == 503


def test_a_parked_request_does_not_delay_shutdown(tmp_path, history):
    pol = tmp_path / "policy.toml"
    pol.write_text("")
    srv = make_server("127.0.0.1", 0, history, pol, stale_after_s=600, clock=lambda: T0,
                      claims_path=tmp_path / "claims.sqlite", users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    claim = _book(base)
    threading.Thread(target=lambda: _get(f"{base}/api/claims/{claim['id']}/events",
                                         timeout=20), daemon=True).start()
    for _ in range(100):
        if srv.watchers.attached(claim["id"]):
            break
        time.sleep(0.02)
    began = time.monotonic()
    srv.shutdown()
    srv.server_close()
    assert time.monotonic() - began < 5


def test_posting_to_the_events_route_is_not_a_write(live):
    srv, base = live
    claim = _book(base)
    assert _post(f"{base}/api/claims/{claim['id']}/events", {})[0] in (404, 405)


def test_the_watcher_is_woken_when_the_booking_is_cancelled(live):
    srv, base = live
    claim = _book(base)
    got = []
    t = threading.Thread(target=lambda: got.append(
        _get(f"{base}/api/claims/{claim['id']}/events", timeout=20)))
    t.start()
    for _ in range(100):
        if srv.watchers.attached(claim["id"]):
            break
        time.sleep(0.02)
    assert _post(f"{base}/api/claims/{claim['id']}/cancel", {})[0] == 200
    t.join(20)
    assert got[0][0] == 200
    assert got[0][1]["event"] == "cancelled"


def test_a_watched_victim_is_taken_at_once_over_http(live):
    """The integration the unit tests cannot reach: claims.create() must ask the server's
    own Watchers registry whether a victim is listening. With that wiring missing, every
    victim keeps the full grace_minutes and an important booking revokes nothing."""
    srv, base = live
    victim = _book(base)                       # lendable, notice_seconds=0
    got = []
    t = threading.Thread(target=lambda: got.append(
        _get(f"{base}/api/claims/{victim['id']}/events", timeout=20)))
    t.start()
    for _ in range(100):
        if srv.watchers.attached(victim["id"]):
            break
        time.sleep(0.02)
    status, body = _post(base + "/api/claims",
                         {"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 79,
                          "duration_minutes": 60, "priority": "important"})
    assert status == 201
    assert body["claim"]["start"] == T0.isoformat()      # now, not now + 30 minutes
    assert [r["id"] for r in body["revoked"]] == [victim["id"]]
    t.join(20)
    assert got[0][1]["event"] == "taken"
