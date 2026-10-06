import pytest
from resourcemonitor.policy import load_policy


def _write(tmp_path, body):
    p = tmp_path / "policy.toml"
    p.write_text(body)
    return p


GOOD = """
[assignments]
"dorian.zwanzig" = [0, 1]
"cwinkelmann" = [2, 3]
[rules]
idle_util_pct = 5
idle_min_mib = 1024
idle_grace_s = 1800
capacity_free_mib = 40960
[notify]
cooldown_s = 3600
channel = "#gpu-watch"
"""


def test_loads_assignments_as_sets_of_int(tmp_path):
    pol = load_policy(_write(tmp_path, GOOD))

    assert pol.assignments["dorian.zwanzig"] == frozenset({0, 1})
    assert pol.capacity_free_mib == 40960
    assert pol.cooldown_s == 3600


def test_owner_of_gpu_maps_an_index_to_its_assignee(tmp_path):
    pol = load_policy(_write(tmp_path, GOOD))

    assert pol.owner_of_gpu(1) == "dorian.zwanzig"
    assert pol.owner_of_gpu(7) is None, "an unassigned GPU belongs to nobody"


def test_overlapping_assignments_are_rejected(tmp_path):
    """Two users owning the same GPU makes 'out of allocation' undecidable."""
    body = GOOD.replace('"cwinkelmann" = [2, 3]', '"cwinkelmann" = [1, 2]')

    with pytest.raises(ValueError, match="both assigned GPU 1"):
        load_policy(_write(tmp_path, body))


def test_unknown_gpu_index_is_rejected(tmp_path):
    body = GOOD.replace("[0, 1]", "[0, 99]")

    with pytest.raises(ValueError, match="99"):
        load_policy(_write(tmp_path, body))


def test_missing_section_names_the_section(tmp_path):
    with pytest.raises(ValueError, match="rules"):
        load_policy(_write(tmp_path, '[assignments]\n"a" = [0]\n'))
