import re
from pathlib import Path

WEB = Path("resourcemonitor/web")


def test_no_inline_script_or_style_because_csp_forbids_it():
    html = (WEB / "index.html").read_text()
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None
    assert "<style" not in html and " style=" not in html
    assert 'src="/app.js"' in html and 'href="/app.css"' in html


def test_no_external_urls_anywhere():
    for f in ("index.html", "app.js", "app.css"):
        assert re.search(r"https?://", (WEB / f).read_text()) is None, f


def test_data_never_goes_through_innerHTML():
    js = (WEB / "app.js").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js


def test_page_has_the_three_sections_and_caveat_slot():
    html = (WEB / "index.html").read_text()
    for id_ in ("now", "timeline", "usage", "timeseries", "caveats", "stale-banner"):
        assert f'id="{id_}"' in html


def test_dry_run_slack_line_is_on_the_page_and_per_alert_status_only_when_posting():
    html = (WEB / "index.html").read_text()
    assert "Slack posting is off (dry run) — alerts are shown here only." in html
    js = (WEB / "app.js").read_text()
    assert 'd.slack !== "dry-run"' in js and 'd.slack === "posting"' in js
