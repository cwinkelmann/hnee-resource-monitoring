from pathlib import Path

from resourcemonitor.cli import build_parser


def test_posting_is_opt_in():
    """The default must be dry-run: a misconfigured first start should not spam."""
    assert build_parser().parse_args(["once"]).post is False
    assert build_parser().parse_args(["once", "--post"]).post is True


def test_watch_takes_an_interval():
    assert build_parser().parse_args(["watch", "--interval", "30"]).interval == 30


def test_report_is_a_mode():
    """`report` reads the ledger and does not poll, so it is safe to run any time."""
    assert build_parser().parse_args(["report"]).mode == "report"


def test_watch_loop_never_echoes_exception_message(monkeypatch, capsys, tmp_path):
    """A bad webhook URL can end up in an exception message; it must not reach the journal."""
    import pytest
    from resourcemonitor import cli

    policy = Path(__file__).parent.parent / "deploy" / "policy.example.toml"

    def boom(*a, **k):
        raise ValueError("unknown url type: 'SECRET-URL'")

    class Stop(Exception):
        pass

    def stop(_):
        raise Stop

    monkeypatch.setattr(cli, "run_once", boom)
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(Stop):
        cli.main(["watch", "--policy", str(policy), "--state", str(tmp_path / "s.json"),
                  "--energy", str(tmp_path / "e.json")])
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert "ValueError" in out


def test_post_rejects_non_https_url_without_echoing_it(monkeypatch, tmp_path):
    import pytest
    from resourcemonitor import cli

    policy = Path(__file__).parent.parent / "deploy" / "policy.example.toml"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "hooks.slack.com/SECRET")
    with pytest.raises(SystemExit) as ei:
        cli.main(["once", "--post", "--policy", str(policy),
                  "--state", str(tmp_path / "s.json"), "--energy", str(tmp_path / "e.json")])
    assert "SECRET" not in str(ei.value)


def test_run_once_gates_repeat_alerts_and_saves_energy(monkeypatch, tmp_path):
    """Same snapshot twice: the second run must send nothing; the ledger is persisted."""
    from datetime import datetime, timezone
    from resourcemonitor import cli
    from resourcemonitor.energy import EnergyLedger
    from resourcemonitor.model import GpuProcess, GpuState, Snapshot
    from resourcemonitor.policy import load_policy
    from resourcemonitor.rules import IdleTracker
    from resourcemonitor.state import State

    pol = load_policy(Path(__file__).parent.parent / "deploy" / "policy.example.toml")
    owner = next(iter(pol.assignments))
    gpu = next(iter(pol.assignments[owner]))
    other = next(u for u in pol.assignments if u != owner)
    snap = Snapshot(datetime.now(timezone.utc),
                    (GpuState(gpu, 81559, 22715, 100, 500.0),),
                    (GpuProcess(1, gpu, 22706, other),))
    monkeypatch.setattr(cli, "probe", lambda: snap)

    class Rec:
        dry_run = False

        def __init__(self):
            self.sent = []

        def send(self, alerts, host):
            self.sent.append(list(alerts))
            return True

    rec = Rec()
    state = State.load(tmp_path / "s.json")
    ledger = EnergyLedger()
    tracker = IdleTracker()
    epath = tmp_path / "e.json"

    first = cli.run_once(pol, state, rec, tracker, ledger, "h", epath)
    second = cli.run_once(pol, state, rec, tracker, ledger, "h", epath)

    assert first >= 1 and len(rec.sent) == 1
    assert second == 0 and len(rec.sent) == 1
    assert epath.exists()


def _history_setup(monkeypatch, tmp_path):
    """Fake probe producing one alert, a recording notifier, and fresh run state."""
    import dataclasses
    from datetime import datetime, timedelta, timezone
    from resourcemonitor import cli
    from resourcemonitor.energy import EnergyLedger
    from resourcemonitor.model import GpuProcess, GpuState, Snapshot
    from resourcemonitor.policy import load_policy
    from resourcemonitor.rules import IdleTracker
    from resourcemonitor.state import State

    pol = load_policy(Path(__file__).parent.parent / "deploy" / "policy.example.toml")
    owner = next(iter(pol.assignments))
    gpu = next(iter(pol.assignments[owner]))
    other = next(u for u in pol.assignments if u != owner)
    snap = Snapshot(datetime.now(timezone.utc),
                    (GpuState(gpu, 81559, 22715, 100, 500.0),),
                    (GpuProcess(1, gpu, 22706, other),))
    ticks = iter(range(1, 1000))

    def fake_probe():            # each poll gets its own timestamp, as in real life
        return dataclasses.replace(snap, taken_at=snap.taken_at + timedelta(seconds=next(ticks)))

    monkeypatch.setattr(cli, "probe", fake_probe)

    class Rec:
        dry_run = False

        def __init__(self):
            self.sent = []
            self.ok = True

        def send(self, alerts, host):
            self.sent.append(list(alerts))
            return self.ok

    return (pol, State.load(tmp_path / "s.json"), Rec(), IdleTracker(), EnergyLedger(),
            tmp_path / "e.json")


def test_run_once_records_history_with_sent_flags(tmp_path, monkeypatch):
    import sqlite3
    from resourcemonitor import cli
    from resourcemonitor.history import HistoryWriter
    pol, state, rec, tracker, ledger, epath = _history_setup(monkeypatch, tmp_path)
    hist = HistoryWriter(tmp_path / "h.sqlite")
    cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath, history=hist)
    cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath, history=hist)
    hist.close()
    rows = sqlite3.connect(tmp_path / "h.sqlite").execute(
        "SELECT ts, sent FROM alerts ORDER BY rowid").fetchall()
    assert [s for _, s in rows] == [1, 0]                     # sent once, then suppressed


def _sent_flags(tmp_path, monkeypatch, *, dry_run, ok):
    import sqlite3
    from resourcemonitor import cli
    from resourcemonitor.history import HistoryWriter
    pol, state, rec, tracker, ledger, epath = _history_setup(monkeypatch, tmp_path)
    rec.dry_run, rec.ok = dry_run, ok
    hist = HistoryWriter(tmp_path / "h.sqlite")
    fresh = cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath, history=hist)
    hist.close()
    assert fresh == 1 and len(rec.sent) == 1                 # the notifier was still called
    return [s for (s,) in sqlite3.connect(tmp_path / "h.sqlite").execute(
        "SELECT sent FROM alerts ORDER BY rowid")]


def test_a_dry_run_never_records_an_alert_as_sent(tmp_path, monkeypatch):
    assert _sent_flags(tmp_path, monkeypatch, dry_run=True, ok=True) == [0]


def test_a_failed_slack_post_is_not_recorded_as_sent(tmp_path, monkeypatch):
    assert _sent_flags(tmp_path, monkeypatch, dry_run=False, ok=False) == [0]


def test_the_cooldown_still_applies_after_a_dry_run(tmp_path, monkeypatch):
    from resourcemonitor import cli
    pol, state, rec, tracker, ledger, epath = _history_setup(monkeypatch, tmp_path)
    rec.dry_run = True
    assert cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath) == 1
    assert cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath) == 0


def test_watch_stores_the_slack_mode(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    from resourcemonitor import cli

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_once", stop)
    policy = Path(__file__).parent.parent / "deploy" / "policy.example.toml"
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch", "--policy", str(policy), "--state", str(tmp_path / "s.json"),
                  "--energy", str(tmp_path / "e.json"), "--history", str(tmp_path / "h.sqlite")])
    assert sqlite3.connect(tmp_path / "h.sqlite").execute(
        "SELECT value FROM meta WHERE key='slack'").fetchall() == [("dry-run",)]


def test_a_broken_history_never_stops_alerts(tmp_path, monkeypatch, capsys):
    import sqlite3
    from resourcemonitor import cli

    class Exploding:
        def record(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error at /secret/path")

    pol, state, rec, tracker, ledger, epath = _history_setup(monkeypatch, tmp_path)
    sent = cli.run_once(pol, state, rec, tracker, ledger, "carrot", epath,
                        history=Exploding())
    assert sent >= 1 and rec.sent                           # alerts went out
    out = capsys.readouterr().out
    assert "history write failed: OperationalError" in out and "/secret/path" not in out


def test_history_flags_parse():
    a = build_parser().parse_args(["watch", "--retention-days", "30", "--no-history"])
    assert a.retention_days == 30 and a.no_history is True


def test_watch_survives_a_history_constructor_failure(tmp_path, monkeypatch, capsys):
    import sqlite3
    import pytest
    from resourcemonitor import cli

    def boom(*a, **k):
        raise sqlite3.OperationalError("unable to open /secret/path")

    seen = []

    def fake_run_once(*a, **k):
        seen.append(k.get("history", "missing"))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "HistoryWriter", boom)
    monkeypatch.setattr(cli, "run_once", fake_run_once)
    policy = Path(__file__).parent.parent / "deploy" / "policy.example.toml"
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch", "--policy", str(policy), "--state", str(tmp_path / "s.json"),
                  "--energy", str(tmp_path / "e.json"),
                  "--history", str(tmp_path / "h.sqlite")])
    assert seen == [None]
    out = capsys.readouterr().out
    assert "history disabled: OperationalError" in out and "/secret/path" not in out
