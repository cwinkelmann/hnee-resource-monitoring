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


def test_both_units_run_in_the_conda_env():
    for unit in ("resourcemonitor.service", "resourcemonitor-web.service"):
        body = (ROOT / "deploy" / unit).read_text()
        assert "ExecStart=%h/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor" in body, unit
        assert "/usr/bin/python3" not in body, unit


def test_web_unit_binds_the_lan_address_and_never_posts():
    unit = (ROOT / "deploy/resourcemonitor-web.service").read_text()
    assert "serve --bind 10.188.1.1 --port 8765" in unit
    assert "--post" not in unit and "EnvironmentFile" not in unit


def test_deploy_skill_covers_the_dashboard():
    body = (SKILLS / "deploy-resourcemonitor" / "SKILL.md").read_text()
    assert "resourcemonitor-web" in body and "8765" in body
