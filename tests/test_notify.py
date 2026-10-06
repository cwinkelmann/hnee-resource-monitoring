import json
import pytest

from resourcemonitor.notify import Notifier, build_payload
from resourcemonitor.rules import Alert

A = Alert(kind="booked_gpu", key="booking:other:dorian.zwanzig:6", gpu_index=6,
          user="dorian.zwanzig",
          text="dorian.zwanzig is using GPU 6 (22.2 GiB), which is assigned to cwinkelmann.")


def test_payload_mentions_the_user_so_slack_notifies_them():
    body = json.dumps(build_payload([A], host="carrot"))

    assert "dorian.zwanzig" in body
    assert "carrot" in body


def test_payload_groups_several_alerts_into_one_message():
    """Two violations are one notification, not two pings."""
    payload = build_payload([A, A], host="carrot")

    assert json.dumps(payload).count("GPU 6") == 2
    assert isinstance(payload["blocks"], list)


def test_dry_run_never_opens_the_network(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry run must not call urlopen")

    monkeypatch.setattr("urllib.request.urlopen", explode)

    assert Notifier("https://hooks.slack.test/x", dry_run=True).send([A], "carrot") is True


def test_the_webhook_url_never_appears_in_the_repr():
    """It is a bearer credential; a traceback or log line must not leak it."""
    n = Notifier("https://hooks.slack.com/services/T/B/SECRET", dry_run=True)

    assert "SECRET" not in repr(n)


def test_empty_alert_list_sends_nothing(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))

    assert Notifier("https://x", dry_run=False).send([], "carrot") is False


def test_new_kinds_have_their_own_icon():
    from resourcemonitor.notify import ICON
    assert ICON["unattributed"] and ICON["report"] == ":zap:"


def test_every_alert_kind_produced_by_the_rules_has_an_icon():
    """A kind without an icon falls back to a question mark, which hides what the alert is."""
    import inspect
    import re

    from resourcemonitor import cli, rules
    from resourcemonitor.notify import ICON

    produced = set(re.findall(r'kind="([a-z_]+)"', inspect.getsource(rules)))
    produced |= set(re.findall(r'kind="([a-z_]+)"', inspect.getsource(cli)))

    assert {"over_booking", "booked_gpu", "idle", "capacity", "unattributed",
            "report"} <= produced
    assert produced <= set(ICON)
    assert "allocation" not in ICON
