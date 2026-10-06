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


from resourcemonitor.probe import label_for


def test_apps_query_reads_the_process_name_column():
    procs = parse_apps_query("GPU-ggg, 3578603, 77014 MiB, VLLM::Worker_TP0\n", {"GPU-ggg": 0})
    assert procs[0].name == "VLLM::Worker_TP0" and procs[0].used_mib == 77014


def test_apps_query_still_accepts_the_three_column_format():
    procs = parse_apps_query("GPU-ggg, 3078913, 22706 MiB\n", {"GPU-ggg": 6})
    assert procs[0].name is None and procs[0].pid == 3078913


def test_a_comma_in_the_process_path_does_not_break_parsing():
    procs = parse_apps_query("GPU-ggg, 7, 10 MiB, /opt/a,b/python\n", {"GPU-ggg": 1})
    assert procs[0].name == "/opt/a,b/python"


def test_label_shows_the_python_script_but_no_arguments():
    cmd = ["/opt/kev/.venv/bin/python", "/app/scripts/kev_run.py", "_train_epochs",
           "--token", "SECRET123", "--batch", "8"]
    assert label_for("/opt/kev/.venv/bin/python", cmd) == "python kev_run.py"


def test_label_shows_python_module():
    assert label_for("python", ["python3.12", "-m", "vllm.entrypoints.openai.api_server",
                                "--api-key", "SECRET"]) == "python -m vllm.entrypoints.openai.api_server"


def test_label_falls_back_to_the_nvidia_process_name():
    assert label_for("VLLM::Worker_TP0", ["VLLM::Worker_TP0"]) == "VLLM::Worker_TP0"
    assert label_for("/usr/bin/blender", None) == "blender"
    assert label_for(None, None) is None


def test_label_is_capped_at_60_chars():
    assert len(label_for("x" * 200, None)) == 60


def _py(*args):
    return label_for("python", ["python", *args])


def test_dash_m_after_the_script_is_a_script_argument_not_a_module():
    assert _py("train.py", "-m", "SECRET") == "python train.py"


def test_dash_m_without_a_module_or_with_an_option_as_module_is_plain_python():
    assert _py("-m") == "python"
    label = _py("-m", "--token", "SECRET")
    assert label == "python" and "SECRET" not in label


def test_module_must_look_like_a_dotted_identifier():
    assert _py("-m", "a/b;rm") == "python"
    assert _py("-m", "pkg.mod") == "python -m pkg.mod"


def test_dash_c_code_is_never_shown():
    assert _py("-c", "x='a.py'") == "python"
    assert _py("-u", "-c", "token='abc.py'") == "python"


def test_a_py_looking_option_value_is_not_the_script():
    label = _py("--token", "abc.py", "run.py")
    assert "abc" not in label
    assert _py("-X", "dev", "run.py") == "python run.py"
    assert _py("-W", "ignore", "-u", "run.py") == "python run.py"


def test_script_name_must_be_a_plain_py_basename():
    assert _py("/a/b/we ird;.py") == "python"
    assert _py("notes.txt", "x.py") == "python"


def test_empty_and_nul_only_cmdlines():
    assert label_for(None, []) is None
    assert label_for(None, [""]) is None
    assert label_for("python", []) == "python"


def test_rewritten_argv0_does_not_leak_arguments():
    assert "token" not in (label_for("gunicorn: worker [--token x]", None) or "")
    assert "token" not in (label_for(None, ["gunicorn: worker [--token x]"]) or "")
    assert label_for("bad;name", None) is None


def test_cmdline_of_nul_only_is_none(monkeypatch, tmp_path):
    import builtins, io
    from resourcemonitor import probe as pr
    real = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda p, *a, **k: io.BytesIO(b"\0\0") if str(p).startswith("/proc/") else real(p, *a, **k))
    assert pr.cmdline_of(1) is None
