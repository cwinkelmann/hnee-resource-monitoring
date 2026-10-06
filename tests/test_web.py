import json
import shutil
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from resourcemonitor.claims import ClaimsStore
from resourcemonitor.web import make_server
from tests.claims_fixture import FakeUsers, book
from tests.history_fixture import build_history

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=48)
POLICY = """[assignments]
"dorian.zwanzig" = [0, 1, 2, 3]
"cwinkelmann" = [4, 5, 6, 7]
[rules]
idle_util_pct = 5
idle_min_mib = 1024
idle_grace_s = 1800
capacity_free_mib = 40960
[notify]
cooldown_s = 3600
channel = "#gpu-watch"
"""


_HISTORY_TEMPLATE = None


@pytest.fixture(scope="module", autouse=True)
def _history_template(tmp_path_factory):
    """The 48 h fixture history is built once per module; each test gets its own copy."""
    global _HISTORY_TEMPLATE
    path = tmp_path_factory.mktemp("history") / "h.sqlite"
    build_history(path, T0, hours=48)
    _HISTORY_TEMPLATE = path
    yield path
    _HISTORY_TEMPLATE = None


def _serve(tmp_path, with_history=True, now=NOW, seed=False):
    hist = tmp_path / "h.sqlite"
    if with_history:
        shutil.copy(_HISTORY_TEMPLATE, hist)
    pol = tmp_path / "policy.toml"; pol.write_text(POLICY)
    claims = tmp_path / "claims.sqlite"
    if seed:
        store = ClaimsStore(claims, users=FakeUsers())
        book(store, user="cwinkelmann", gpu=6, gib=40, start=NOW - timedelta(hours=1), hours=4,
             now=NOW - timedelta(hours=1))
        store.close()
    srv = make_server("127.0.0.1", 0, hist, pol, stale_after_s=600, clock=lambda: now,
                      claims_path=claims, users=FakeUsers())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def live(tmp_path):
    srv, base = _serve(tmp_path, seed=True)
    yield base
    srv.shutdown()


def test_index_and_assets_are_served_with_security_headers(live):
    for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")]:
        status, headers, _ = _get(live + path)
        assert status == 200 and headers["Content-Type"].startswith(ctype)
        assert headers["Content-Security-Policy"] == "default-src 'self'"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers


def test_api_now_returns_live_view(live):
    status, _, body = _get(live + "/api/now")
    d = json.loads(body)
    assert status == 200 and d["stale"] is False and "assigned_to" not in d["gpus"][6]
    g6 = d["gpus"][6]
    assert [b["user"] for b in g6["bookings"]] == ["cwinkelmann"]
    assert g6["booked_mib"] == 40960 and g6["free_mib"] == g6["total_mib"] - 40960
    assert d["gpus"][5]["bookings"] == [] and d["gpus"][5]["free_mib"] == d["gpus"][5]["total_mib"]


def _assert_headers(headers):
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers


def test_api_claims_lists_bookings_and_users_lists_the_directory(live):
    status, headers, body = _get(live + "/api/claims")
    d = json.loads(body)
    assert status == 200 and d["now"] == NOW.isoformat()
    assert [c["user"] for c in d["claims"]] == ["cwinkelmann"] and d["claims"][0]["gpu"] == 6
    _assert_headers(headers)
    status, headers, body = _get(live + "/api/claims?days=1")
    assert status == 200 and len(json.loads(body)["claims"]) == 1
    status, headers, body = _get(live + "/api/users")
    assert status == 200 and json.loads(body) == {"users": sorted(FakeUsers().all())}
    _assert_headers(headers)
    _assert_headers(_get(live + "/api/now")[1])


@pytest.mark.parametrize("q", ["/api/claims?days=15", "/api/claims?days=0", "/api/claims?days=x",
                               "/api/claims?days=-1", "/api/claims?days=" + "9" * 5000,
                               "/api/claims?days=014", "/api/claims?foo=1", "/api/users?x=1",
                               "/api/claims?back=0", "/api/claims?back=31", "/api/claims?back=abc",
                               "/api/claims?back=999", "/api/claims?back=-1", "/api/claims?back="])
def test_claims_and_users_bad_params_are_400(live, q):
    status, headers, body = _get(live + q)
    assert status == 400 and json.loads(body) == {"error": "bad request"}
    _assert_headers(headers)


def test_missing_claims_file_means_no_bookings_not_an_error(tmp_path):
    srv, base = _serve(tmp_path)
    try:
        status, headers, body = _get(base + "/api/claims")
        assert status == 200 and json.loads(body)["claims"] == []
        _assert_headers(headers)
        status, headers, body = _get(base + "/api/now")
        assert status == 200 and json.loads(body)["gpus"][6]["bookings"] == []
        _assert_headers(headers)
    finally:
        srv.shutdown()


def test_api_usage_defaults_and_params(live):
    status, _, body = _get(live + "/api/usage?from=2026-10-05&to=2026-10-06&by=day")
    d = json.loads(body)
    assert status == 200 and len(d["periods"]) == 2 and len(d["caveats"]) == 2


@pytest.mark.parametrize("q", [
    "/api/usage?from=2026-13-45", "/api/usage?by=month", "/api/usage?from=2020-01-01&to=2026-10-06",
    "/api/usage?from=2026-10-06&to=2026-10-01", "/api/timeseries?hours=-1",
    "/api/timeseries?hours=abc", "/api/timeseries?hours=169",
    "/api/timeline?hours=0", "/api/timeline?hours=721",
    "/api/vram?hours=0", "/api/vram?hours=169", "/api/vram?hours=abc", "/api/vram?hours=9999",
    "/api/vram?hours=0024", "/api/vram?hours=", "/api/vram?hours=24&hours=24", "/api/vram?x=1"])
def test_bad_params_are_400_without_a_traceback(live, q):
    status, _, body = _get(live + q)
    assert status == 400 and json.loads(body) == {"error": "bad request"}
    assert b"Traceback" not in body


def test_api_timeline_returns_jobs_per_gpu(live):
    status, _, body = _get(live + "/api/timeline?hours=48")
    d = json.loads(body)
    assert status == 200 and d["gpus"]["6"] and d["gpus"]["6"][0]["name"] == "python kev_run.py"


def test_unknown_path_is_404_and_post_is_405(live):
    assert _get(live + "/../../etc/passwd")[0] == 404
    assert _get(live + "/api/now", method="POST")[0] == 405
    assert _get(live + "/api/vram", method="POST")[0] == 405


def test_api_vram_returns_stacked_vram_per_gpu(live):
    status, headers, body = _get(live + "/api/vram")
    d = json.loads(body)
    assert status == 200 and set(d) == {"from", "to", "gpus"}
    assert d["to"] == NOW.isoformat() and d["from"] == (NOW - timedelta(hours=24)).isoformat()
    assert set(d["gpus"]) == {str(i) for i in range(8)}
    g7 = d["gpus"]["7"]
    assert g7["total_mib"] == 81559 and 0 < len(g7["points"]) <= 300
    assert g7["points"][-1]["by_user"] == {"dorian.zwanzig": 22715 + 512}
    assert d["gpus"]["4"]["points"][-1]["by_user"] == {"(unattributed)": 2048}
    _assert_headers(headers)
    status, _, body = _get(live + "/api/vram?hours=1")
    assert status == 200 and len(json.loads(body)["gpus"]["7"]["points"]) == 12


def test_fresh_install_without_history_is_503_not_500(tmp_path):
    srv, base = _serve(tmp_path, with_history=False)
    try:
        status, _, body = _get(base + "/api/now")
        assert status == 503 and json.loads(body) == {"error": "no history yet"}
        status, headers, body = _get(base + "/api/vram")
        assert status == 503 and json.loads(body) == {"error": "no history yet"}
        _assert_headers(headers)
        assert _get(base + "/healthz")[0] == 503
        assert _get(base + "/")[0] == 200                  # the page itself still loads
    finally:
        srv.shutdown()


def test_stopped_monitor_is_reported_stale(tmp_path):
    srv, base = _serve(tmp_path, now=NOW + timedelta(hours=2))
    try:
        assert json.loads(_get(base + "/api/now")[2])["stale"] is True
    finally:
        srv.shutdown()


def test_serve_does_not_need_a_webhook(monkeypatch):
    from resourcemonitor.cli import build_parser
    a = build_parser().parse_args(["serve", "--bind", "10.188.1.1", "--port", "8765"])
    assert (a.mode, a.bind, a.port) == ("serve", "10.188.1.1", 8765)


def test_web_module_never_imports_probe_or_notify():
    import ast, pathlib
    src = pathlib.Path("resourcemonitor/web.py").read_text()
    names = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("probe" in m or "notify" in m) for m in names)


_SERVE_PROBE = """
import runpy, sys
sys.argv = ["resourcemonitor", "serve", "--help"]
try:
    runpy.run_module("resourcemonitor", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
loaded = sorted(m for m in sys.modules if m in ("resourcemonitor.probe", "resourcemonitor.notify"))
print("LOADED=" + ",".join(loaded))
"""


def test_python_m_resourcemonitor_serve_never_imports_probe_or_notify():
    """The real entry path of the deployed unit (`python -m resourcemonitor serve`)."""
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).parent.parent
    r = subprocess.run([sys.executable, "-c", _SERVE_PROBE], cwd=root,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "--bind" in r.stdout and "--stale-after" in r.stdout   # serve's own help ran
    assert "LOADED=\n" in r.stdout


def test_serve_parser_keeps_the_deployed_flags_and_defaults():
    from resourcemonitor.web import build_parser
    a = build_parser().parse_args([])
    assert (a.bind, a.port, a.stale_after) == ("127.0.0.1", 8765, 180)
    assert a.history.name == "history.sqlite" and a.policy.name == "policy.toml"
    a = build_parser().parse_args(["--bind", "10.188.1.1", "--port", "8765"])
    assert (a.bind, a.port) == ("10.188.1.1", 8765)


def test_cli_serve_delegates_to_the_web_entry_point(monkeypatch):
    from resourcemonitor import cli, web
    seen = []
    monkeypatch.setattr(web, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["serve", "--bind", "10.188.1.1", "--port", "9", "--stale-after", "5",
                     "--history", "/x/h.sqlite", "--policy", "/x/p.toml",
                     "--claims", "/x/c.sqlite"]) == 0
    assert seen == [["--bind", "10.188.1.1", "--port", "9", "--history", "/x/h.sqlite",
                     "--policy", "/x/p.toml", "--stale-after", "5",
                     "--claims", "/x/c.sqlite"]]


def test_favicon_svg_is_served_and_favicon_ico_is_204(live):
    status, headers, body = _get(live + "/favicon.svg")
    assert status == 200 and headers["Content-Type"].startswith("image/svg+xml")
    assert body.lstrip().startswith(b"<svg")
    status, headers, body = _get(live + "/favicon.ico")
    assert status == 204 and body == b""
    for h in (headers,):
        assert h["Content-Security-Policy"] == "default-src 'self'"
        assert h["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/api/usage", "/api/timeline", "/api/timeseries", "/api/vram",
                                  "/api/now"])
def test_existing_db_with_zero_polls_is_503_no_history(tmp_path, path):
    from resourcemonitor.history import HistoryWriter
    HistoryWriter(tmp_path / "h.sqlite").close()
    srv, base = _serve(tmp_path, with_history=False)
    try:
        status, _, body = _get(base + path)
        assert status == 503 and json.loads(body) == {"error": "no history yet"}
    finally:
        srv.shutdown()


def test_idle_connections_time_out():
    from resourcemonitor.web import _Handler
    assert _Handler.timeout == 15


class _Slots:
    def __init__(self, free):
        self.free, self.timeouts, self.released = free, [], 0

    def acquire(self, blocking=True, timeout=None):
        self.timeouts.append(timeout)
        return self.free

    def release(self):
        self.released += 1


@pytest.mark.parametrize("path", ["/api/timeline", "/api/usage", "/api/timeseries", "/api/vram"])
def test_heavy_queries_answer_busy_when_no_slot_frees_up(live, monkeypatch, path):
    from resourcemonitor import web
    slots = _Slots(free=False)
    monkeypatch.setattr(web, "_QUERY_SLOTS", slots)
    status, headers, body = _get(live + path)
    assert status == 503 and json.loads(body) == {"error": "busy"}
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert slots.timeouts == [2] and slots.released == 0


def test_heavy_queries_release_their_slot_and_now_is_not_gated(live, monkeypatch):
    from resourcemonitor import web
    slots = _Slots(free=True)
    monkeypatch.setattr(web, "_QUERY_SLOTS", slots)
    assert _get(live + "/api/timeline?hours=24")[0] == 200
    assert _get(live + "/api/timeline?hours=0")[0] == 400      # bad input still releases
    assert slots.released == len(slots.timeouts) == 2
    assert _get(live + "/api/now")[0] == 200 and len(slots.timeouts) == 2


def test_query_slots_are_a_bounded_semaphore_of_two():
    import threading
    from resourcemonitor import web
    assert isinstance(web._QUERY_SLOTS, type(threading.BoundedSemaphore(2)))
    assert web._QUERY_SLOTS._initial_value == 2


def test_api_claims_back_reaches_further_into_the_past_than_the_default_week(tmp_path):
    srv, base = _serve(tmp_path, seed=True)
    try:
        then = NOW - timedelta(days=20)
        store = ClaimsStore(tmp_path / "claims.sqlite", users=FakeUsers())
        book(store, user="andre.kliem", gpu=2, gib=30, start=then, hours=6, now=then)
        store.close()
        users = lambda q: [c["user"] for c in json.loads(_get(base + "/api/claims" + q)[2])["claims"]]
        assert users("") == ["cwinkelmann"]                      # default: 7 days back
        assert users("?days=1") == ["cwinkelmann"]
        assert users("?back=30") == ["andre.kliem", "cwinkelmann"]
        assert users("?days=1&back=30") == ["andre.kliem", "cwinkelmann"]
        assert users("?back=19") == ["cwinkelmann"]
        status, headers, _ = _get(base + "/api/claims?days=1&back=30")
        assert status == 200
        _assert_headers(headers)
    finally:
        srv.shutdown()
