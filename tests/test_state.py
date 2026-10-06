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
