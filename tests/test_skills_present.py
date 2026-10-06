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
