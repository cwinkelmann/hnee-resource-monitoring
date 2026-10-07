"""The web server's write path: POST /api/claims and /api/claims/<id>/cancel."""
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from resourcemonitor import web
from resourcemonitor.queries import open_ro
from resourcemonitor.web import make_server
from tests import history_fixture
from tests.claims_fixture import T0, FakeUsers
from tests.history_fixture import build_history

S = "2026-10-06T14:00:00+02:00"
E = "2026-10-06T18:00:00+02:00"
SECURITY_STATUSES = (201, 400, 403, 409, 413, 415, 429)


def _booking(**over):
    d = {"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 40, "start": S, "end": E}
    d.update(over)
    return d


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    path = tmp_path_factory.mktemp("hist") / "h.sqlite"
    build_history(path, T0 - timedelta(hours=2), hours=2)
    return path


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """The limiter is per process; give every test its own so the suite never trips it."""
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter())


def _start(tmp_path, history_path):
    pol = tmp_path / "policy.toml"
    pol.write_text("")
    srv = make_server("127.0.0.1", 0, history_path, pol, stale_after_s=600,
                      clock=lambda: T0, claims_path=tmp_path / "claims.sqlite",
                      users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def live(tmp_path, history):
    srv, base = _start(tmp_path, history)
    yield base
    srv.shutdown()
    srv.server_close()


def _post(url, obj, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_create_then_cancel_round_trip(live):
    s, _, body = _post(live + "/api/claims", {"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 40,
                       "start": "2026-10-06T14:00:00+02:00", "end": "2026-10-06T18:00:00+02:00",
                       "note": "KEV eval"})
    c = json.loads(body)["claim"]
    assert s == 201 and c["start"] == "2026-10-06T12:00:00+00:00" and c["vram_mib"] == 40960
    s, _, body = _post(live + f"/api/claims/{c['id']}/cancel", {})
    assert s == 200 and json.loads(body)["claim"]["cancelled_ip"] == "127.0.0.1"
    s, _, body = _post(live + f"/api/claims/{c['id']}/cancel", {})
    assert s == 200                                              # idempotent


def test_created_booking_is_listed_and_records_the_socket_ip_not_forwarded_for(live):
    s, _, body = _post(live + "/api/claims", _booking(note=None),
                       headers={"X-Forwarded-For": "10.9.9.9"})
    assert s == 201 and json.loads(body)["claim"]["created_ip"] == "127.0.0.1"
    with urllib.request.urlopen(live + "/api/claims", timeout=5) as r:
        assert [c["user"] for c in json.loads(r.read())["claims"]] == ["dorian.zwanzig"]


@pytest.mark.parametrize("payload, status, detail", [
    ({"user": "dorain", "gpu": 4, "vram_gib": 1, "start": S, "end": E}, 400, "unknown user 'dorain'"),
    ({"user": "dorian.zwanzig", "gpu": "4", "vram_gib": 1, "start": S, "end": E}, 400, "missing or invalid field 'gpu'"),
    ({"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 1, "start": "2026-10-06T14:00", "end": E}, 400, "times need a date, time and UTC offset"),
    ({"user": "dorian.zwanzig", "gpu": 4, "vram_gib": 1, "start": S, "end": E, "x": 1}, 400, "unknown field"),
])
def test_bad_bookings_get_a_specific_400(live, payload, status, detail):
    s, _, body = _post(live + "/api/claims", payload)
    assert (s, json.loads(body)) == (status, {"error": "invalid", "detail": detail})


@pytest.mark.parametrize("payload, detail", [
    ({"gpu": 4, "vram_gib": 1, "start": S, "end": E}, "missing or invalid field 'user'"),
    (_booking(gpu=True), "missing or invalid field 'gpu'"),
    (_booking(vram_gib=0), "missing or invalid field 'vram_gib'"),
    (_booking(vram_gib="40"), "missing or invalid field 'vram_gib'"),
    (_booking(vram_gib=1e308), "missing or invalid field 'vram_gib'"),
    (_booking(vram_gib=10**309), "missing or invalid field 'vram_gib'"),
    (_booking(end=None), "missing or invalid field 'end'"),
    (_booking(note=5), "missing or invalid field 'note'"),
    (_booking(gpu=9), "GPU must be 0–7"),
    (_booking(user="<script>"), "invalid user name"),
])
def test_field_errors_name_only_fixed_fields_and_never_echo_input(live, payload, detail):
    s, _, body = _post(live + "/api/claims", payload)
    assert (s, json.loads(body)) == (400, {"error": "invalid", "detail": detail})


@pytest.mark.parametrize("raw", [b"[1, 2]", b"not json", b"\xff\xfe", b"[" * 2000 + b"]" * 2000,
                                 b'{"vram_gib": Infinity}'],
                         ids=["list", "garbage", "bad-utf", "deep-nesting", "infinity"])
def test_body_that_is_not_a_json_object_is_400(live, raw):
    s, _, body = _post(live + "/api/claims", None, raw=raw)
    if raw.startswith(b"{"):                                    # parses, but the field is bad
        assert s == 400 and json.loads(body)["error"] == "invalid"
    else:
        assert (s, json.loads(body)) == (400, {"error": "invalid",
                                               "detail": "body must be a JSON object"})


def test_overbooking_is_409(live):
    assert _post(live + "/api/claims", _booking(vram_gib=60))[0] == 201
    s, _, body = _post(live + "/api/claims", _booking(vram_gib=60))
    d = json.loads(body)
    assert s == 409 and d["error"] == "conflict" and "unbooked between" in d["detail"]


def test_wrong_content_type_is_415(live):
    s, _, body = _post(live + "/api/claims", _booking(), headers={"Content-Type": "text/plain"})
    assert (s, json.loads(body)) == (415, {"error": "unsupported media type"})
    s, _, _ = _post(live + "/api/claims", _booking(),
                    headers={"Content-Type": "application/json; charset=utf-8"})
    assert s == 201


def test_foreign_origin_is_403(live):
    s, _, body = _post(live + "/api/claims", _booking(), headers={"Origin": "http://evil.example"})
    assert (s, json.loads(body)) == (403, {"error": "forbidden"})
    s, _, _ = _post(live + "/api/claims", _booking(), headers={"Origin": live})
    assert s == 201                                     # live is http://127.0.0.1:<port>


def test_oversize_body_is_413(live):
    s, _, body = _post(live + "/api/claims", None, raw=b" " * 5000)
    assert (s, json.loads(body)) == (413, {"error": "too large"})


def _raw_post(base, body, ctype="application/json", origin=None, length=None, delay=0.0):
    """Send a POST over a bare socket and read the whole response; a reset raises."""
    import socket
    import time
    from urllib.parse import urlsplit
    u = urlsplit(base)
    s = socket.create_connection((u.hostname, u.port), timeout=5)
    try:
        h = (f"POST /api/claims HTTP/1.1\r\nHost: {u.netloc}\r\nContent-Type: {ctype}\r\n"
             f"Content-Length: {len(body) if length is None else length}\r\n")
        if origin:
            h += f"Origin: {origin}\r\n"
        s.sendall((h + "\r\n").encode())
        if delay:
            time.sleep(delay)
        s.sendall(body)
        time.sleep(0.2)                         # let the server reply and close first
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        return out
    finally:
        s.close()


@pytest.mark.parametrize("kw, status", [
    ({"body": b" " * 5000, "ctype": "text/plain"}, 415),
    ({"body": b" " * 5000, "ctype": "text/plain", "delay": 0.1}, 415),
    ({"body": b" " * 5000, "origin": "http://evil.example"}, 403),
    ({"body": b" " * 50000, "origin": "http://evil.example"}, 403),
    ({"body": b" " * 60000}, 413),
], ids=["415", "415-late-body", "403", "403-50k", "413-60k"])
def test_rejected_writes_reply_without_a_connection_reset(live, kw, status):
    out = _raw_post(live, **kw)                 # a ConnectionResetError here fails the test
    head, _, body = out.partition(b"\r\n\r\n")
    assert head.split(b" ")[1] == str(status).encode() and body.endswith(b"}")


def test_drain_stops_after_one_second_against_a_dribbling_client(live):
    import socket
    import time
    from urllib.parse import urlsplit
    u = urlsplit(live)
    s = socket.create_connection((u.hostname, u.port), timeout=5)
    s.setblocking(False)
    try:
        s.sendall((f"POST /api/claims HTTP/1.1\r\nHost: {u.netloc}\r\n"
                   "Content-Type: text/plain\r\nContent-Length: 65536\r\n\r\n").encode())
        t0 = time.monotonic()
        out = b""
        while time.monotonic() - t0 < 4:
            try:
                s.send(b" ")                    # one byte every 0.3 s, never the whole body
            except OSError:
                break
            time.sleep(0.3)
            try:
                chunk = s.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            out += chunk
            if not chunk or out.endswith(b"}"):
                break
        elapsed = time.monotonic() - t0
    finally:
        s.close()
    assert elapsed < 2.0 and out.startswith(b"HTTP/1.0 415 ")


def test_missing_content_length_is_413(live):
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(live)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        conn.putrequest("POST", "/api/claims")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        r = conn.getresponse()
        assert (r.status, json.loads(r.read())) == (413, {"error": "too large"})
    finally:
        conn.close()


def test_rate_limit_is_429_after_30_writes(live, monkeypatch):
    assert web._RateLimiter().limit == 30 and web._RateLimiter().window_s == 3600
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter(limit=2))
    assert _post(live + "/api/claims", _booking(gpu=1))[0] == 201
    assert _post(live + "/api/claims", _booking(gpu=2))[0] == 201
    s, _, body = _post(live + "/api/claims", _booking(gpu=3))
    assert (s, json.loads(body)) == (429, {"error": "too many requests"})


def test_rate_limiter_window_rolls_and_is_per_ip():
    lim = web._RateLimiter(limit=2, window_s=3600)
    assert lim.allow("a", 0) and lim.allow("a", 1) and not lim.allow("a", 2)
    assert lim.allow("b", 2)                                    # another client is unaffected
    assert lim.allow("a", 3600.5) and not lim.allow("a", 3600.9)  # only the oldest aged out


def test_unknown_cancel_id_is_404_and_bad_path_is_404(live):
    s, _, body = _post(live + "/api/claims/999/cancel", {})
    assert (s, json.loads(body)) == (404, {"error": "not found", "detail": "no such booking"})
    s, _, body = _post(live + "/api/claims/abc/cancel", {})
    assert (s, json.loads(body)) == (404, {"error": "not found"})
    assert _post(live + "/api/claims/1234567890/cancel", {})[0] == 404
    assert _post(live + "/api/nope", {})[0] == 404


def test_post_to_read_routes_is_405(live):
    s, _, body = _post(live + "/api/now", {})
    assert (s, json.loads(body)) == (405, {"error": "method not allowed"})
    assert _post(live + "/", {})[0] == 405


def test_booking_works_without_history_using_card_fallback(tmp_path):
    srv, base = _start(tmp_path, tmp_path / "absent.sqlite")
    try:
        assert _post(base + "/api/claims", _booking(gpu=0, vram_gib=79))[0] == 201
        s, _, body = _post(base + "/api/claims", _booking(gpu=1, vram_gib=80))
        assert (s, json.loads(body)) == (400, {"error": "invalid",
                                               "detail": "VRAM must be between 1 and 79 GiB"})
    finally:
        srv.shutdown()
        srv.server_close()


def test_card_total_comes_from_the_latest_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(history_fixture, "TOTAL_MIB", 40 * 1024)
    hist = tmp_path / "small.sqlite"
    build_history(hist, T0 - timedelta(hours=1), hours=1)
    srv, base = _start(tmp_path, hist)
    try:
        s, _, body = _post(base + "/api/claims", _booking(vram_gib=41))
        assert (s, json.loads(body)["detail"]) == (400, "VRAM must be between 1 and 40 GiB")
    finally:
        srv.shutdown()
        srv.server_close()


def test_every_write_response_carries_the_security_headers(live, monkeypatch):
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter(limit=6))
    seen = {
        201: _post(live + "/api/claims", _booking(vram_gib=60)),
        409: _post(live + "/api/claims", _booking(vram_gib=60)),
        400: _post(live + "/api/claims", _booking(gpu="x")),
        403: _post(live + "/api/claims", _booking(), headers={"Origin": "http://evil.example"}),
        415: _post(live + "/api/claims", _booking(), headers={"Content-Type": "text/plain"}),
        413: _post(live + "/api/claims", None, raw=b" " * 5000),
        429: _post(live + "/api/claims", _booking()),
    }
    assert set(seen) == set(SECURITY_STATUSES)
    for status, (s, headers, _) in seen.items():
        assert s == status
        assert headers["Content-Security-Policy"] == "default-src 'self'"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert not any(k.lower().startswith("access-control-") for k in headers)


_SERVE_PROBE = """
import runpy, sys
sys.argv = ["resourcemonitor", "serve", "--help"]
try:
    runpy.run_module("resourcemonitor", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
import resourcemonitor.web                         # the module that now holds the write path
bad = ("resourcemonitor.probe", "resourcemonitor.notify", "resourcemonitor.cli")
print("LOADED=" + ",".join(sorted(m for m in sys.modules if m in bad)))
"""


def test_history_stays_unwritable_and_serve_imports_nothing_forbidden(tmp_path, history):
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).parent.parent
    r = subprocess.run([sys.executable, "-c", _SERVE_PROBE], cwd=root,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "--bind" in r.stdout and "LOADED=\n" in r.stdout
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    srv, base = _start(tmp_path, history)
    try:
        assert _post(base + "/api/claims", _booking())[0] == 201
    finally:
        srv.shutdown()
        srv.server_close()
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before
    assert (tmp_path / "claims.sqlite").exists()
    conn = open_ro(history)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM polls")
    finally:
        conn.close()


# ---------- quick booking: POST /api/claims/quick ----------
def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_quick_books_then_releases(live):
    s, _, body = _post(live + "/api/claims/quick", {"user": "dorian.zwanzig", "gpu": 3})
    c = json.loads(body)["claim"]
    assert s == 201 and (c["kind"], c["vram_mib"], c["note"]) == ("quick", 81559, "quick booking")
    assert c["start"] == T0.isoformat() and c["end"] == "2026-10-07T07:00:00+00:00"
    s, _, body = _post(live + "/api/claims/quick", {"user": "andre.kliem", "gpu": 3})
    assert s == 201
    s, _, body = _post(live + "/api/claims/quick", {"user": None, "gpu": 3})
    assert (s, json.loads(body)) == (200, {"claim": None})
    s, _, body = _post(live + "/api/claims/quick", {"user": None, "gpu": 3})
    assert (s, json.loads(body)) == (200, {"claim": None})            # nothing to release
    claims = _get(live + "/api/claims")["claims"]
    assert [(x["user"], x["cancelled_ip"]) for x in claims] == [
        ("dorian.zwanzig", "127.0.0.1"), ("andre.kliem", "127.0.0.1")]


def test_quick_bookings_appear_in_now_with_their_kind(live):
    assert _post(live + "/api/claims/quick", {"user": "dorian.zwanzig", "gpu": 3})[0] == 201
    assert _post(live + "/api/claims", _booking(gpu=5, vram_gib=10))[0] == 201
    gpus = {g["gpu"]: g for g in _get(live + "/api/now")["gpus"]}
    assert [(b["user"], b["kind"]) for b in gpus[3]["bookings"]] == [("dorian.zwanzig", "quick")]
    assert [b["kind"] for b in gpus[5]["bookings"]] == ["calendar"]


@pytest.mark.parametrize("payload, detail", [
    ({"user": "dorian.zwanzig", "gpu": 3, "vram_gib": 4}, "unknown field"),
    ({"user": "dorian.zwanzig", "gpu": True}, "missing or invalid field 'gpu'"),
    ({"user": "dorian.zwanzig", "gpu": "3"}, "missing or invalid field 'gpu'"),
    ({"user": "dorian.zwanzig"}, "missing or invalid field 'gpu'"),
    ({"gpu": 3}, "missing or invalid field 'user'"),
    ({"user": 5, "gpu": 3}, "missing or invalid field 'user'"),
    ({"user": "dorain", "gpu": 3}, "unknown user 'dorain'"),
    ({"user": "<script>", "gpu": 3}, "invalid user name"),
    ({"user": "dorian.zwanzig", "gpu": 8}, "GPU must be 0–7"),
])
def test_quick_bad_requests_are_400(live, payload, detail):
    s, _, body = _post(live + "/api/claims/quick", payload)
    assert (s, json.loads(body)) == (400, {"error": "invalid", "detail": detail})


def test_quick_is_409_while_a_calendar_booking_is_active(live):
    assert _post(live + "/api/claims", _booking(gpu=3, vram_gib=10))[0] == 201
    s, _, body = _post(live + "/api/claims/quick", {"user": "andre.kliem", "gpu": 3})
    assert (s, json.loads(body)) == (409, {"error": "conflict",
                                           "detail": "GPU 3 has calendar bookings — use the calendar"})


def test_quick_rejects_foreign_origin_and_non_json(live):
    url = live + "/api/claims/quick"
    q = {"user": "dorian.zwanzig", "gpu": 3}
    s, _, body = _post(url, q, headers={"Origin": "http://evil.example"})
    assert (s, json.loads(body)) == (403, {"error": "forbidden"})
    s, _, body = _post(url, q, headers={"Content-Type": "text/plain"})
    assert (s, json.loads(body)) == (415, {"error": "unsupported media type"})
    s, _, body = _post(url, None, raw=b" " * 5000)
    assert (s, json.loads(body)) == (413, {"error": "too large"})
    s, _, body = _post(url, None, raw=b"[]")
    assert (s, json.loads(body)["detail"]) == (400, "body must be a JSON object")


def test_quick_counts_against_the_write_rate_limit(live, monkeypatch):
    monkeypatch.setattr(web, "_WRITES", web._RateLimiter(limit=1))
    assert _post(live + "/api/claims/quick", {"user": "dorian.zwanzig", "gpu": 3})[0] == 201
    s, _, body = _post(live + "/api/claims/quick", {"user": None, "gpu": 3})
    assert (s, json.loads(body)) == (429, {"error": "too many requests"})


def test_quick_responses_carry_the_security_headers(live):
    url = live + "/api/claims/quick"
    for payload, status in (({"user": "dorian.zwanzig", "gpu": 3}, 201), ({"user": None, "gpu": 3}, 200),
                            ({"user": None, "gpu": True}, 400)):
        s, headers, _ = _post(url, payload)
        assert s == status
        assert headers["Content-Security-Policy"] == "default-src 'self'"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert not any(k.lower().startswith("access-control-") for k in headers)


def test_get_on_quick_is_404(live):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(live + "/api/claims/quick", timeout=5)
    assert e.value.code == 404


# ---------- VRAM is optional in POST /api/claims ----------
@pytest.mark.parametrize("over", [{}, {"vram_gib": None}], ids=["absent", "null"])
def test_booking_without_vram_takes_the_whole_card(live, over):
    payload = _booking(**over)
    if not over:
        del payload["vram_gib"]
    s, _, body = _post(live + "/api/claims", payload)
    assert s == 201 and json.loads(body)["claim"]["vram_mib"] == 81559


def test_whole_card_booking_against_an_existing_share_is_409(live):
    assert _post(live + "/api/claims", _booking(user="andre.kliem", vram_gib=1))[0] == 201
    payload = _booking()
    del payload["vram_gib"]
    s, _, body = _post(live + "/api/claims", payload)
    assert s == 409 and "unbooked between" in json.loads(body)["detail"]


def test_whole_card_booking_uses_the_latest_poll_total(tmp_path, monkeypatch):
    monkeypatch.setattr(history_fixture, "TOTAL_MIB", 40 * 1024)
    hist = tmp_path / "small.sqlite"
    build_history(hist, T0 - timedelta(hours=1), hours=1)
    srv, base = _start(tmp_path, hist)
    try:
        s, _, body = _post(base + "/api/claims", _booking(vram_gib=None))
        assert (s, json.loads(body)["claim"]["vram_mib"]) == (201, 40 * 1024)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route, payload", [
    ("/api/claims", _booking(gpu=10**20)),
    ("/api/claims/quick", {"user": "dorian.zwanzig", "gpu": 10**20}),
], ids=["calendar", "quick"])
def test_huge_gpu_number_is_a_400_not_a_500(live, route, payload):
    s, _, body = _post(live + route, payload)
    assert (s, json.loads(body)) == (400, {"error": "invalid", "detail": "GPU must be 0–7"})


# --- lendable vs important ---------------------------------------------------------------

def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_priority_defaults_to_lendable_and_rejects_other_values(live):
    s, _, body = _post(live + "/api/claims", _booking())
    assert s == 201 and json.loads(body)["claim"]["priority"] == "lendable"
    assert json.loads(body)["takes"] == [] and json.loads(body)["adjusted_start"] is None
    for bad in ("urgent", 1, None):
        s, _, body = _post(live + "/api/claims", _booking(priority=bad, gpu=5))
        assert (s, json.loads(body)["detail"]) == (400, "priority must be lendable or important")


def test_important_over_a_running_lendable_is_moved_and_reports_the_take(live):
    s, _, body = _post(live + "/api/claims", _booking(vram_gib=70))
    lend = json.loads(body)["claim"]
    s, _, body = _post(live + "/api/claims", _booking(user="cwinkelmann", vram_gib=40,
                                                       priority="important"))
    r = json.loads(body)
    assert s == 201 and r["claim"]["priority"] == "important"
    assert r["adjusted_start"] == "2026-10-06T12:00:00+00:00"
    assert r["claim"]["start"] == "2026-10-06T12:30:00+00:00"
    (t,) = r["takes"]
    assert (t["id"], t["user"], t["start"], t["end"]) == (
        lend["id"], "dorian.zwanzig", "2026-10-06T12:30:00+00:00", "2026-10-06T16:00:00+00:00")
    assert t["vram_gib"] == round(t["vram_mib"] / 1024, 1) and t["vram_mib"] > 0

    listing = _get(live + "/api/claims")
    assert listing["grace_minutes"] == 30
    got = {c["id"]: c for c in listing["claims"]}
    (seg,) = got[lend["id"]]["taken"]
    assert seg["by"] == ["cwinkelmann"] and seg["vram_mib"] == t["vram_mib"]
    assert got[r["claim"]["id"]]["taken"] == []


def test_important_conflict_names_the_kind_of_shortfall(live):
    _post(live + "/api/claims", _booking(vram_gib=60, priority="important"))
    s, _, body = _post(live + "/api/claims", _booking(user="cwinkelmann", vram_gib=60,
                                                       priority="important"))
    assert s == 409 and "not booked as important" in json.loads(body)["detail"]


def test_now_reports_effective_shares_and_taken_segments(live):
    _post(live + "/api/claims", _booking(vram_gib=70))
    _post(live + "/api/claims", _booking(user="cwinkelmann", vram_gib=40, priority="important",
                                         start="2026-10-06T15:00:00+02:00"))
    gpu = next(g for g in _get(live + "/api/now")["gpus"] if g["gpu"] == 4)
    (b,) = gpu["bookings"]                       # the important one has not started yet
    assert b["priority"] == "lendable" and b["effective_mib"] == 70 * 1024
    assert gpu["booked_mib"] == 70 * 1024
    (seg,) = b["taken"]
    assert seg["start"] == "2026-10-06T13:00:00+00:00" and seg["by"] == ["cwinkelmann"]


def test_now_names_the_host_by_its_short_name(live):
    import socket
    assert _get(live + "/api/now")["host"] == socket.gethostname().split(".")[0]
