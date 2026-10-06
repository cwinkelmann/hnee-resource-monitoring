"""`python -m resourcemonitor`. `serve` is dispatched before cli is imported, so the
dashboard process never loads probe or notify."""
import sys


def dispatch(argv: list[str]) -> int:
    if argv[:1] == ["serve"]:
        from resourcemonitor.web import main as serve_main
        return serve_main(argv[1:])
    from resourcemonitor.cli import main
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(dispatch(sys.argv[1:]))
