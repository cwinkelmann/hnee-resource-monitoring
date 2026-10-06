from datetime import datetime, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import check_allocation, check_capacity

POL = Policy(
    assignments={"dorian.zwanzig": frozenset({0, 1, 2, 3}),
                 "cwinkelmann": frozenset({4, 5, 6, 7})},
    idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
    capacity_free_mib=40960, cooldown_s=3600, channel="#gpu-watch",
)


def _snap(gpus, procs):
    return Snapshot(datetime.now(timezone.utc), tuple(gpus), tuple(procs))


def test_flags_a_user_running_outside_their_assignment():
    """The real 2026-10-06 situation: dorian on GPUs 6 and 7."""
    snap = _snap(
        [GpuState(6, 81559, 22715, 100), GpuState(7, 81559, 27367, 98)],
        [GpuProcess(3078913, 6, 22706, "dorian.zwanzig"),
         GpuProcess(3079828, 7, 27358, "dorian.zwanzig")],
    )

    alerts = check_allocation(snap, POL)

    assert len(alerts) == 2
    assert {a.gpu_index for a in alerts} == {6, 7}
    assert all(a.user == "dorian.zwanzig" for a in alerts)
    assert all("cwinkelmann" in a.text for a in alerts), "say whose GPU it is"


def test_a_user_on_their_own_gpus_is_not_flagged():
    snap = _snap([GpuState(5, 81559, 40000, 90)],
                 [GpuProcess(1, 5, 40000, "cwinkelmann")])

    assert check_allocation(snap, POL) == []


def test_an_unassigned_gpu_is_not_an_allocation_violation():
    """A GPU nobody owns is free-for-all; flagging it would be noise."""
    pol = Policy(assignments={"cwinkelmann": frozenset({4})}, idle_util_pct=5,
                 idle_min_mib=1024, idle_grace_s=1800, capacity_free_mib=40960,
                 cooldown_s=3600, channel="#c")
    snap = _snap([GpuState(0, 81559, 100, 50)], [GpuProcess(1, 0, 100, "someone")])

    assert check_allocation(snap, pol) == []


def test_an_unattributed_process_is_not_accused():
    """user is None when /proc is gone. Never guess an owner into an accusation."""
    snap = _snap([GpuState(6, 81559, 22715, 100)], [GpuProcess(1, 6, 22715, None)])

    assert check_allocation(snap, POL) == []


def test_allocation_key_is_stable_per_user_and_gpu():
    snap = _snap([GpuState(6, 81559, 1, 1)], [GpuProcess(1, 6, 1, "dorian.zwanzig")])
    later = _snap([GpuState(6, 81559, 2, 2)], [GpuProcess(99, 6, 2, "dorian.zwanzig")])

    # same user, same GPU, different PID and memory -> one incident, not two
    assert check_allocation(snap, POL)[0].key == check_allocation(later, POL)[0].key


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


def test_unattributed_process_on_an_assigned_gpu_is_reported_non_accusingly():
    from resourcemonitor.rules import check_unattributed
    snap = _snap([GpuState(6, 81559, 22715, 100)], [GpuProcess(9, 6, 22706, None)])

    alerts = check_unattributed(snap, POL)

    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind == "unattributed" and a.key == "unattributed:6"
    assert a.user is None and a.gpu_index == 6
    assert "could not be resolved" in a.text and "cwinkelmann" in a.text


def test_unattributed_ignores_small_attributed_and_unassigned():
    from resourcemonitor.rules import check_unattributed
    pol = Policy(assignments={"a": frozenset({0})}, idle_util_pct=5, idle_min_mib=1024,
                 idle_grace_s=1800, capacity_free_mib=40960, cooldown_s=3600, channel="#x")
    snap = _snap([GpuState(0, 81559, 0, 0), GpuState(1, 81559, 0, 0)],
                 [GpuProcess(1, 0, 100, None),        # below idle_min_mib
                  GpuProcess(2, 0, 5000, "a"),        # attributed
                  GpuProcess(3, 1, 5000, None)])      # GPU has no assignee

    assert check_unattributed(snap, pol) == []


def test_two_processes_of_one_user_on_one_gpu_are_one_allocation_alert():
    """The live 2026-10-06 case: dorian's job plus a helper on GPU 7 showed twice."""
    from resourcemonitor.rules import check_allocation as alloc
    snap = _snap([GpuState(7, 81559, 37069, 98)],
                 [GpuProcess(10, 7, 27358, "dorian.zwanzig"),
                  GpuProcess(11, 7, 9702, "dorian.zwanzig")])

    (a,) = alloc(snap, POL)

    assert a.key == "allocation:dorian.zwanzig:7"
    assert "GPU 7 (36.2 GiB)" in a.text                  # 27358 + 9702 MiB, summed


def test_two_unattributed_processes_on_one_gpu_are_one_alert():
    from resourcemonitor.rules import check_unattributed
    snap = _snap([GpuState(4, 81559, 6144, 0)],
                 [GpuProcess(20, 4, 2048, None), GpuProcess(21, 4, 4096, None)])

    (a,) = check_unattributed(snap, POL)

    assert a.key == "unattributed:4"
    assert "holding 6.0 GiB on GPU 4" in a.text
