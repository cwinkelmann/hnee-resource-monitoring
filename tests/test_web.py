import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from resourcemonitor.web import make_server
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


def _serve(tmp_path, with_history=True, now=NOW):
    hist = tmp_path / "h.sqlite"
    if with_history:
        build_history(hist, T0, hours=48)
    pol = tmp_path / "policy.toml"; pol.write_text(POLICY)
    srv = make_server("127.0.0.1", 0, hist, pol, stale_after_s=600, clock=lambda: now)
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
    srv, base = _serve(tmp_path)
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
    assert status == 200 and d["stale"] is False and d["gpus"][6]["assigned_to"] == "cwinkelmann"


def test_api_usage_defaults_and_params(live):
    status, _, body = _get(live + "/api/usage?from=2026-10-05&to=2026-10-06&by=day")
    d = json.loads(body)
    assert status == 200 and len(d["periods"]) == 2 and len(d["caveats"]) == 2


@pytest.mark.parametrize("q", [
    "/api/usage?from=2026-13-45", "/api/usage?by=month", "/api/usage?from=2020-01-01&to=2026-10-06",
    "/api/usage?from=2026-10-06&to=2026-10-01", "/api/timeseries?hours=-1",
    "/api/timeseries?hours=abc", "/api/timeseries?hours=169",
    "/api/timeline?hours=0", "/api/timeline?hours=721"])
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


def test_fresh_install_without_history_is_503_not_500(tmp_path):
    srv, base = _serve(tmp_path, with_history=False)
    try:
        status, _, body = _get(base + "/api/now")
        assert status == 503 and json.loads(body) == {"error": "no history yet"}
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
                     "--history", "/x/h.sqlite", "--policy", "/x/p.toml"]) == 0
    assert seen == [["--bind", "10.188.1.1", "--port", "9", "--history", "/x/h.sqlite",
                     "--policy", "/x/p.toml", "--stale-after", "5"]]


def test_favicon_svg_is_served_and_favicon_ico_is_204(live):
    status, headers, body = _get(live + "/favicon.svg")
    assert status == 200 and headers["Content-Type"].startswith("image/svg+xml")
    assert body.lstrip().startswith(b"<svg")
    status, headers, body = _get(live + "/favicon.ico")
    assert status == 204 and body == b""
    for h in (headers,):
        assert h["Content-Security-Policy"] == "default-src 'self'"
        assert h["X-Content-Type-Options"] == "nosniff"
