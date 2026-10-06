import getpass
import os
from pathlib import Path

import pytest
from resourcemonitor.probe import owner_of


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs Linux /proc")
def test_resolves_the_owner_of_a_live_process():
    assert owner_of(os.getpid()) == getpass.getuser()


def test_a_dead_pid_returns_none_rather_than_raising():
    """Processes exit between the nvidia-smi call and the /proc read. That is normal."""
    assert owner_of(2 ** 22) is None


def test_an_unresolvable_uid_returns_none_not_a_number(monkeypatch):
    """Inside a rootless container every foreign UID maps to the overflow UID.

    The tool must say 'unattributed' there rather than inventing a name -- reporting
    the wrong person is worse than reporting nobody. See the plan's Measured facts:
    host uid=1053 dorian.zwanzig becomes uid=65534 nobody in a rootless container.
    """
    import resourcemonitor.probe as probe

    monkeypatch.setattr(probe, "_uid_of", lambda pid: 65534)
    monkeypatch.setattr(probe, "_name_of_uid", lambda uid: None)

    assert probe.owner_of(1234) is None


def test_the_overflow_uid_is_never_resolved_to_a_name(monkeypatch):
    """pwd resolves 65534 to 'nobody', which would be a guess. _name_of_uid is NOT patched."""
    import resourcemonitor.probe as probe

    monkeypatch.setattr(probe, "_uid_of", lambda pid: 65534)

    assert probe.owner_of(1234) is None
