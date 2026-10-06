from datetime import datetime, timezone
from resourcemonitor.probe import parse_gpu_query, parse_apps_query
from resourcemonitor.model import Snapshot

GPU_CSV = """\
0, GPU-aaa, 81559 MiB, 0 MiB, 0 %, 63.80 W
6, GPU-ggg, 81559 MiB, 22715 MiB, 100 %, 575.00 W
7, GPU-hhh, 81559 MiB, 27367 MiB, 98 %, 556.90 W
"""

APPS_CSV = """\
GPU-ggg, 3078913, 22706 MiB
GPU-hhh, 3079828, 27358 MiB
"""


def test_parses_units_off_every_numeric_field():
    """nvidia-smi appends ' MiB' and ' %'; a naive int() raises on all of them."""
    gpus = parse_gpu_query(GPU_CSV)

    assert [g.index for g in gpus] == [0, 6, 7]
    assert gpus[1].used_mib == 22715
    assert gpus[1].util_pct == 100
    assert gpus[0].total_mib == 81559


def test_power_is_a_float_not_an_int():
    """63.80 W truncated to 63 loses 1.3% of an idle card and compounds over a week."""
    gpus = parse_gpu_query(GPU_CSV)

    assert gpus[0].power_w == 63.80
    assert gpus[1].power_w == 575.00


def test_power_draw_may_be_unsupported_and_reads_as_zero():
    """Some cards report '[N/A]'. That must not take the whole poll down."""
    gpus = parse_gpu_query("0, GPU-aaa, 81559 MiB, 0 MiB, 0 %, [N/A]\n")

    assert gpus[0].power_w == 0.0


def test_processes_join_to_gpus_by_uuid_not_order():
    """The apps query reports a UUID. Zipping by position silently mis-attributes."""
    uuid_to_index = {"GPU-ggg": 6, "GPU-hhh": 7}

    procs = parse_apps_query(APPS_CSV, uuid_to_index)

    assert {(p.pid, p.gpu_index) for p in procs} == {(3078913, 6), (3079828, 7)}


def test_a_process_on_an_unknown_uuid_is_dropped_not_guessed():
    procs = parse_apps_query("GPU-zzz, 999, 10 MiB\n", {"GPU-ggg": 6})

    assert procs == ()


def test_free_mib_is_total_minus_used():
    snap = Snapshot(datetime.now(timezone.utc), parse_gpu_query(GPU_CSV), ())

    assert snap.free_mib(0) == 81559
    assert snap.free_mib(6) == 81559 - 22715


def test_empty_apps_query_means_no_processes_not_an_error():
    """An idle box returns an empty string; that is the common case, not a failure."""
    assert parse_apps_query("", {"GPU-ggg": 6}) == ()


def test_parses_a_real_recording_from_the_gpu_host():
    """Fixtures recorded from carrot; assert only structure, not volatile readings."""
    from pathlib import Path
    from resourcemonitor.probe import parse_gpu_uuids

    fx = Path(__file__).parent / "fixtures"
    gpu_text = (fx / "real_gpu_query.txt").read_text()
    uuids = parse_gpu_uuids(gpu_text)
    gpus = parse_gpu_query(gpu_text)
    procs = parse_apps_query((fx / "real_apps_query.txt").read_text(), uuids)

    assert len(gpus) == 8
    assert sorted(g.index for g in gpus) == list(range(8))
    assert all(g.power_w > 0 for g in gpus)
    assert procs
    assert all(p.gpu_index in range(8) for p in procs)


def test_na_memory_cell_does_not_lose_the_poll():
    from resourcemonitor.probe import _num
    assert _num("[N/A]") == 0
    assert _num("[Not Supported]") == 0
    assert _num("22715 MiB") == 22715


def test_uid_is_parsed_from_the_status_uid_line():
    from resourcemonitor.probe import _parse_status_uid
    status = "Name:\tpython\nUmask:\t0022\nUid:\t1053\t1053\t1053\t1053\nGid:\t1\t1\t1\t1\n"
    assert _parse_status_uid(status) == 1053
    assert _parse_status_uid("Name:\tx\n") is None
    assert _parse_status_uid("Uid:\tabc\n") is None
