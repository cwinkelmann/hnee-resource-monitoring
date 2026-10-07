import re
from pathlib import Path

import pytest

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
    for id_ in ("now", "timeline", "usage", "timeseries", "caveats", "stale-banner",
                "bookings", "book-form", "book-error", "booking-calendar", "booking-detail",
                "booking-changes", "vram24", "vram-grid", "vram-legend"):
        assert f'id="{id_}"' in html


def test_bookings_replace_assignments_and_never_use_blocking_dialogs():
    js = (WEB / "app.js").read_text()
    assert "assigned_to" not in js
    assert "confirm(" not in js


def test_dry_run_slack_line_is_on_the_page_and_per_alert_status_only_when_posting():
    html = (WEB / "index.html").read_text()
    assert "Slack posting is off (dry run) — alerts are shown here only." in html
    js = (WEB / "app.js").read_text()
    assert 'd.slack !== "dry-run"' in js and 'd.slack === "posting"' in js


def test_favicon_is_linked_and_self_contained():
    html = (WEB / "index.html").read_text()
    assert '<link rel="icon" href="/favicon.svg"' in html
    svg = (WEB / "favicon.svg").read_text()
    # the SVG namespace is a name, never fetched; nothing else may point outside
    rest = svg.replace('xmlns="http://www.w3.org/2000/svg"', "", 1)
    assert re.search(r"https?://|href=|<image|<script|@import", rest) is None


def _run_js_function(name, call):
    """Extract a top-level helper from app.js and evaluate `call` with node (skip without)."""
    import json, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    js = (WEB / "app.js").read_text()
    m = re.search(r"^  function " + name + r"\(.*?^  \}$", js, re.S | re.M)
    assert m, f"{name} not found in app.js"
    out = subprocess.run([node, "-e", m.group(0) + "\nconsole.log(JSON.stringify(" + call + "));"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_pack_lanes_puts_concurrent_jobs_on_separate_lanes():
    # GPU 7 on the live box: a job and its helper over the same span, then a later job
    r = _run_js_function("packLanes", "packLanes([{start: 0, end: 10}, {start: 0, end: 10},"
                                      " {start: 10, end: 20}, {start: 5, end: 7}])")
    assert r == {"lane": [0, 1, 0, 2], "count": 3}


def test_pack_lanes_keeps_sequential_jobs_on_one_lane_and_never_returns_zero_lanes():
    assert _run_js_function("packLanes", "packLanes([{start: 0, end: 5}, {start: 6, end: 9}])") \
        == {"lane": [0, 0], "count": 1}
    assert _run_js_function("packLanes", "packLanes([])") == {"lane": [], "count": 1}


def test_timeline_ends_ongoing_bars_at_the_last_poll_when_stale():
    js = (WEB / "app.js").read_text()
    assert "state.now.stale" in js and "liveEnd" in js


def test_free_vram_is_the_minimum_over_the_window_counting_only_live_claims_on_that_gpu():
    import shutil, subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "tests/js/test_free_vram.mjs"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_quick_booking_dropdown_posts_to_its_route_without_dialogs():
    js = (WEB / "app.js").read_text()
    assert '"/api/claims/quick"' in js and "confirm(" not in js and "alert(" not in js
    assert 'document.activeElement' in js            # the refresh skips a focused holder select


def test_vram_is_optional_in_the_booking_form():
    html = (WEB / "index.html").read_text()
    tag = re.search(r'<input id="book-vram"[^>]*>', html).group(0)
    assert "required" not in tag and 'placeholder="whole card"' in tag
    assert "VRAM (GiB, optional)" in html


def test_holder_picks_are_debounced_per_gpu_and_flushed_on_enter_or_blur():
    """Arrow keys on a closed select fire change per keystroke; only the last pick is booked."""
    import shutil, subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "tests/js/test_debounce.mjs"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_holder_dropdown_flushes_a_pending_pick_on_enter_and_blur():
    js = (WEB / "app.js").read_text()
    body = re.search(r"^  function holderNode\(.*?^  \}$", js, re.S | re.M).group(0)
    assert '"blur"' in body and '"Enter"' in body and "holderDebounce.push" in body


@pytest.mark.parametrize("bookings, total, hint", [
    ([{"kind": "calendar", "vram_mib": 40960}], 81559, "partly booked — use the calendar"),
    ([{"kind": "calendar", "vram_mib": 40960}, {"kind": "calendar", "vram_mib": 40599}],
     81559, "booked — use the calendar"),
    ([{"kind": "calendar", "vram_mib": 81559}], 81559, "booked — use the calendar"),
])
def test_holder_hint_says_booked_only_when_calendar_bookings_cover_the_card(bookings, total, hint):
    import json
    js = (WEB / "app.js").read_text()
    holder_of = re.search(r"^  function holderOf\(.*?^  \}$", js, re.S | re.M).group(0)
    g = json.dumps({"bookings": bookings, "total_mib": total})
    r = _run_js_function("holderHint", "(" + "function(){" + holder_of
                         + "; return holderHint(holderOf(" + g + "));})()")
    assert r == hint


def test_vram_panel_follows_the_power_panel_and_fetches_its_route():
    html = (WEB / "index.html").read_text()
    assert "<h2>Power &amp; utilisation — last 24 h</h2>" in html
    assert html.index('id="timeseries"') < html.index('id="vram24"')
    assert "<h2>VRAM — last 24 h</h2>" in html
    assert "stacked by user (0–80 GiB) · dashed = booked share" in html
    js = (WEB / "app.js").read_text()
    assert '"/api/vram?hours=24"' in js
    assert "setInterval(loadVram, 300000)" in js


def test_vram_stacking_and_booked_share_helpers():
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "tests/js/test_vram_stack.mjs"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_allocation_rule_in_app_js_matches_the_shared_cases():
    """allocate() in app.js runs the same fixture as claims.allocate; takesPreview builds on it."""
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "tests/js/test_allocate.mjs"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_booking_form_has_an_important_checkbox_off_by_default_and_posts_priority():
    html = (WEB / "index.html").read_text()
    tag = re.search(r'<input id="book-important"[^>]*>', html).group(0)
    assert 'type="checkbox"' in tag and "checked" not in tag
    assert 'id="book-priority-hint"' in html
    js = (WEB / "app.js").read_text()
    assert "payload.priority = formPriority()" in js
    assert "data.grace_minutes" in js and "adjusted_start" in js and "res.body.takes" in js


def test_page_shows_priority_and_taken_segments():
    js = (WEB / "app.js").read_text()
    assert "effective_mib" in js and "c.taken" in js and "b.taken" in js
    assert 'taken: "' in js                                  # an icon for the "taken" alert kind
    css = (WEB / "app.css").read_text()
    assert "repeating-linear-gradient" in css and ".hatch-line" in css and "taken-cut" in css


def test_cpu_ram_card_sits_in_the_now_panel_hidden_until_there_is_data():
    html = (WEB / "index.html").read_text()
    now = html[html.index('id="now"'):html.index('id="bookings"')]
    tag = re.search(r'<div id="box-wrap"[^>]*>', now).group(0)
    assert "hidden" in tag                                   # box is null until the first CPU/RAM poll
    assert 'id="box-card"' in now and 'class="card box-card"' in now
    assert now.index('id="gpu-grid"') < now.index('id="box-wrap"')
    js = (WEB / "app.js").read_text()
    assert "renderBox(d.box)" in js and 'wrap.hidden = !b' in js
    assert '"measured from the next poll"' in js            # cores_busy is null after a restart
    assert "b.swap_total_mib > 0" in js                     # no swap line on a box without swap


def test_cpu_ram_caveats_are_on_the_page():
    html = (WEB / "index.html").read_text()
    assert 'id="box-caveats" class="caveats"' in html
    assert ("Per-user RAM sums each process's resident memory, so memory shared between "
            "processes counts more than once.") in html
    assert "Processes run as root (e.g. many Docker containers) count under system." in html


def test_timeline_has_cpu_and_ram_rows_from_the_server_thresholds():
    js = (WEB / "app.js").read_text()
    assert 'label: "CPU"' in js and 'label: "RAM"' in js
    assert "coresFloorLabel(d.box.cpu_min_cores)" in js and "gibFloorLabel(d.box.ram_min_mib)" in js
    body = re.search(r"^  function renderTimeline\(.*?^  \}$", js, re.S | re.M).group(0)
    assert body.count("packLanes(") == 2                    # overlapping users get lanes, like GPU jobs


def test_system_is_grey_and_never_takes_a_user_colour():
    js = (WEB / "app.js").read_text()
    assert 'const SYSTEM = "system"' in js
    assert 'n === SYSTEM) continue' in js                   # registerUsers skips it
    assert 'user === SYSTEM) return "idle"' in js
    css = (WEB / "app.css").read_text()
    assert css.count("--on-idle:") == 2                     # light and dark


def test_cpu_ram_formatting_helpers():
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "tests/js/test_box.mjs"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
