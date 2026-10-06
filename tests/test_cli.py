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
