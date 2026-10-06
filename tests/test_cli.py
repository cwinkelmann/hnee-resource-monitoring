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
