from datetime import datetime, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import check_capacity

POL = Policy(
    assignments={"dorian.zwanzig": frozenset({0, 1, 2, 3}),
                 "cwinkelmann": frozenset({4, 5, 6, 7})},
    idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
    capacity_free_mib=40960, cooldown_s=3600, channel="#gpu-watch",
)


def _snap(gpus, procs):
    return Snapshot(datetime.now(timezone.utc), tuple(gpus), tuple(procs))


def test_capacity_fires_only_when_no_gpu_has_room():
    tight = [GpuState(i, 81559, 81559 - 1000, 90) for i in range(8)]

    assert len(check_capacity(_snap(tight, []), POL)) == 1


def test_capacity_silent_while_one_gpu_still_has_room():
    gpus = [GpuState(i, 81559, 81559 - 1000, 90) for i in range(7)]
    gpus.append(GpuState(7, 81559, 0, 0))          # one card wide open

    assert check_capacity(_snap(gpus, []), POL) == []


def test_capacity_boundary_is_inclusive():
    """free == threshold counts as available; a job of exactly that size fits."""
    gpus = [GpuState(i, 81559, 81559 - 40960, 50) for i in range(8)]

    assert check_capacity(_snap(gpus, []), POL) == []
