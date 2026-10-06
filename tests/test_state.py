import json
from datetime import datetime, timedelta, timezone

from resourcemonitor.state import State

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def test_first_sighting_sends(tmp_path):
    st = State.load(tmp_path / "s.json")

    assert st.should_send("allocation:dorian:6", T0, 3600) is True


def test_same_incident_is_suppressed_within_the_cooldown(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("k", T0, 3600)

    assert st.should_send("k", T0 + timedelta(minutes=59), 3600) is False


def test_it_sends_again_once_the_cooldown_expires(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("k", T0, 3600)

    assert st.should_send("k", T0 + timedelta(minutes=61), 3600) is True


def test_distinct_incidents_do_not_suppress_each_other(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("allocation:dorian:6", T0, 3600)

    assert st.should_send("allocation:dorian:7", T0, 3600) is True


def test_state_survives_a_restart(tmp_path):
    """systemd restarts the service; a forgotten cooldown means a duplicate alert."""
    p = tmp_path / "s.json"
    st = State.load(p)
    st.should_send("k", T0, 3600)
    st.save()

    assert State.load(p).should_send("k", T0 + timedelta(minutes=5), 3600) is False


def test_a_corrupt_state_file_does_not_crash_the_daemon(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")

    assert State.load(p).should_send("k", T0, 3600) is True


import pytest


@pytest.mark.parametrize("content", ["[1, 2]", '"str"', "null", "42"])
def test_non_dict_json_is_tolerated(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_text(content)

    assert State.load(p).should_send("k", T0, 3600) is True


def test_undecodable_or_unreadable_state_is_tolerated(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b"\xff\xfe\x00bad")
    assert State.load(p).should_send("k", T0, 3600) is True

    d = tmp_path / "dir.json"
    d.mkdir()                       # reading a directory raises OSError
    assert State.load(d).should_send("k", T0, 3600) is True


@pytest.mark.parametrize("stored", ["garbage", "2026-10-06T12:00:00", 5])
def test_unparsable_or_naive_stored_timestamp_counts_as_not_seen(tmp_path, stored):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"k": stored}))

    assert State.load(p).should_send("k", T0, 3600) is True
