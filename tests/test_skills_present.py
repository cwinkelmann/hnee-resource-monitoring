import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude/skills"


@pytest.mark.parametrize("name", ["deploy-resourcemonitor", "gpu-energy-report"])
def test_skill_exists_with_frontmatter(name):
    """Skills must live under .claude/skills/ — .superpowers/skills/ is never loaded."""
    body = (SKILLS / name / "SKILL.md").read_text()

    assert body.startswith("---"), "SKILL.md needs YAML frontmatter"
    assert f"name: {name}" in body
    assert "description:" in body


def test_claude_md_states_the_two_non_negotiables():
    body = (ROOT / "CLAUDE.md").read_text().lower()

    assert "never kills" in body or "observation only" in body
    assert "outside docker" in body or "rootless" in body


UNITS = ("resourcemonitor.service", "resourcemonitor-dry.service",
         "resourcemonitor-web.service")
WRAPPER = ROOT / "deploy/run-resourcemonitor.sh"


@pytest.mark.parametrize("unit", UNITS)
def test_every_unit_starts_through_the_wrapper(unit):
    """systemd cannot take ExecStart's executable from a variable, so the host's
    python and bind address are resolved by the wrapper, not baked into the unit."""
    body = (ROOT / "deploy" / unit).read_text()

    assert "ExecStart=%h/ResourceMonitor/deploy/run-resourcemonitor.sh " in body, unit
    assert "miniconda3" not in body, unit
    assert "/usr/bin/python3" not in body, unit


@pytest.mark.parametrize("unit", UNITS)
def test_no_unit_hardcodes_a_host_address(unit):
    """One unit file has to serve carrot, olive and the t14 alike."""
    body = (ROOT / "deploy" / unit).read_text()

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", body), f"{unit} hardcodes an IP"


def test_web_unit_never_posts():
    unit = (ROOT / "deploy/resourcemonitor-web.service").read_text()
    assert "--post" not in unit and "EnvironmentFile" not in unit


def test_wrapper_reads_the_host_config_and_falls_back_to_system_python():
    guide = WRAPPER.read_text()
    # Comments explain why --post is forwarded, so only real lines may mention it.
    code = "\n".join(l for l in guide.splitlines() if not l.lstrip().startswith("#"))

    assert "host.env" in guide, "the per-host bind/port/python live there"
    assert "python3" in guide, "a host without the conda env must still run"
    assert "--post" not in code, "the wrapper must not hardcode posting"
    assert os.access(WRAPPER, os.X_OK), "systemd needs it executable"


@pytest.mark.parametrize("role,expected", [
    ("serve", ["-m", "resourcemonitor", "serve", "--bind", "10.9.9.9", "--port", "8799"]),
    ("watch", ["-m", "resourcemonitor", "watch", "--interval", "30"]),
])
def test_wrapper_builds_the_command_from_the_environment(role, expected, tmp_path):
    """Run it with a python that only echoes its argv, so we see the real command."""
    echo = tmp_path / "fake-python"
    echo.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    echo.chmod(0o755)

    out = subprocess.run([str(WRAPPER), role], capture_output=True, text=True, env={
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "RESOURCEMONITOR_PYTHON": str(echo),
        "RESOURCEMONITOR_BIND": "10.9.9.9",
        "RESOURCEMONITOR_PORT": "8799",
        "RESOURCEMONITOR_INTERVAL": "30",
    })

    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == expected


def test_wrapper_forwards_extra_arguments(tmp_path):
    """resourcemonitor.service passes --post this way; dropping it would silence
    Slack with no error at all."""
    echo = tmp_path / "fake-python"
    echo.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    echo.chmod(0o755)

    out = subprocess.run([str(WRAPPER), "watch", "--post"], capture_output=True,
                         text=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                         "RESOURCEMONITOR_PYTHON": str(echo)})

    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-1] == "--post"


def test_wrapper_rejects_an_unknown_role(tmp_path):
    out = subprocess.run([str(WRAPPER), "post-everything"], capture_output=True,
                         text=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert out.returncode != 0
    assert "post-everything" in out.stderr or "usage" in out.stderr.lower()


def test_host_env_example_documents_every_variable():
    example = (ROOT / "deploy/host.env.example").read_text()

    for var in ("RESOURCEMONITOR_BIND", "RESOURCEMONITOR_PORT", "RESOURCEMONITOR_PYTHON"):
        assert var in example, var
    assert "SLACK_WEBHOOK_URL" not in example, "the secret stays in its own 0600 file"


def test_deploy_skill_covers_the_dashboard():
    body = (SKILLS / "deploy-resourcemonitor" / "SKILL.md").read_text()
    assert "resourcemonitor-web" in body and "8765" in body
    assert "systemd-run --user --unit=resourcemonitor-soak" in body
    assert "miniconda3/envs/resourcemonitor/bin/python" in body


def test_docs_describe_booking_honestly():
    readme = (ROOT / "README.md").read_text()
    assert "Booking a GPU" in readme and "honour system" in readme and "never enforced" in readme
    claude = (ROOT / "CLAUDE.md").read_text()
    assert "claims.sqlite" in claude


def test_dry_unit_collects_history_without_ever_posting():
    """The dry collector is the real unit minus Slack: no --post, no webhook file."""
    unit = (ROOT / "deploy/resourcemonitor-dry.service").read_text()

    assert "run-resourcemonitor.sh watch" in unit, "the interval is host config now"
    assert "--post" not in unit and "EnvironmentFile" not in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit, "must start at boot, with Linger=yes"


@pytest.mark.parametrize("unit", ["resourcemonitor.service", "resourcemonitor-dry.service",
                                  "resourcemonitor-web.service"])
def test_no_unit_hides_process_owners(unit):
    """In a user unit these imply PrivateUsers=yes, which rewrites foreign UIDs to 65534."""
    body = (ROOT / "deploy" / unit).read_text()

    for hardening in ("ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateUsers"):
        assert f"\n{hardening}=" not in body, f"{unit} sets {hardening}"


def test_guard_script_never_posts_and_prefers_the_units():
    """The no-linger fallback must stay dry, and must say the units are preferred."""
    guard = (ROOT / "deploy/resourcemonitor-guard.sh").read_text()

    code = "\n".join(l for l in guard.splitlines() if not l.lstrip().startswith("#"))

    assert "--post" not in code
    assert "enable-linger" in guard, "must say how to get the supervised path back"
    assert "run-resourcemonitor.sh" in guard, "host config must not be duplicated here"
    assert "miniconda3" not in code, "the interpreter choice belongs to the wrapper"


def test_the_acceptance_script_is_stdlib_only_and_never_posts_to_slack():
    src = Path("deploy/acceptance-slots.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("requests", "SLACK", "webhook", "pynvml"):
        assert banned not in code, banned
    assert "urllib.request" in code
    assert "/api/claims" in code and "/events" in code and "/release" in code
    assert "/api/capacity" in code
