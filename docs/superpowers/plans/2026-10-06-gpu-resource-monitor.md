# GPU Resource Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only script on carrot that attributes GPU occupancy *and electricity consumption* to users, and posts to a Slack channel when someone runs outside their assigned GPUs, holds VRAM while idle, or when no GPU has enough free memory left to start a job.

**Architecture:** One plain Python script, stdlib only, run **on the host, outside Docker** (see "Why not Docker" at the end — a container cannot attribute processes). It runs as a `systemd --user` service, polls `nvidia-smi` and `/proc`, builds an immutable `Snapshot`, evaluates pure rule functions against a TOML policy, accumulates energy between polls, debounces through a state file, and posts to a Slack incoming webhook. The seam that matters is `Snapshot` — every rule is a pure function of it, so the whole rule layer is testable from recorded fixtures with no GPU present.

**Tech Stack:** Python 3.12 (already on carrot), stdlib only — `subprocess`, `tomllib`, `urllib.request`, `dataclasses`, `json`. No third-party packages, no venv.

**Spec:** none — this plan is self-contained. The findings it argues from are recorded in "Measured facts" below; re-measure before contradicting any of them.

## Reaching carrot

Everything in this plan runs on one machine. How to get to it, measured 2026-10-06:

| | |
|---|---|
| address | **`cwinkelmann@10.188.1.1`** |
| hostname | **`carrot` does NOT resolve** — `Host carrot not found: 2(SERVFAIL)`. Always use the IP. |
| auth | key-based; `ssh -o BatchMode=yes` succeeds with no password or prompt |
| hardware | 8 × NVIDIA H100 80GB HBM3, driver 580.178.04 |
| shared with | `dorian.zwanzig` (UID 1053). Split agreed 2026-08-19: dorian 0–3, cwinkelmann 4–7. |

**The interpreter.** The service runs in the `resourcemonitor` conda env (`~/miniconda3/envs/resourcemonitor`, Python 3.12) by the user's choice. The code stays stdlib-only, but **if that env is removed or renamed, the monitor stops**; recreate it with `~/miniconda3/bin/conda create -y -n resourcemonitor python=3.12 pytest`. (Originally this plan hardcoded `/usr/bin/python3`; superseded 2026-10-06.)

`nvidia-smi` is at `/usr/bin/nvidia-smi` and needs no special environment.

**It is a shared machine.** Two consequences for anyone working on this tool: never run
anything heavy on it while testing, and never add a code path that touches another user's
processes (see the Global Constraints).

## Measured facts (carrot, 2026-10-06)

These were established by direct measurement on the box. They are the reason several
decisions below are not negotiable.

| fact | evidence |
|---|---|
| `systemd --user` runs and `Linger=yes` | a user service survives logout; no root needed |
| Python 3.12.3 present, outbound HTTPS works | `api.telegram.org` reachable, so Slack will be |
| Docker is **rootless**, nvidia runtime present | `docker info` → `Context: rootless` |
| `cwinkelmann` subuid range is `951968:65536` | `/etc/subuid` |
| `dorian.zwanzig` is **UID 1053** — outside that range | `id -u` |
| **A rootless container cannot attribute processes** | host: `/proc/3078913` → `uid=1053 name=dorian.zwanzig`; inside `docker run --pid=host`: `uid=65534 name=nobody` |
| **The UID itself is destroyed, not just the name** | `/proc/<pid>/status` → `Uid: 1053` on the host, `Uid: 65534` in the container |
| `nvidia-smi` alone never gives an owner | it reports PIDs only; `ps`/`/proc` supplies the user |
| The agreed split is dorian 0–3, cwinkelmann 4–7 | agreed 2026-08-19 |
| `power.draw` and `power.limit` are queryable | 700 W limit per card (H100 80GB HBM3, driver 580.178.04) |
| **`total_energy_consumption` is NOT supported** | `"not a valid field to query"` — energy must be integrated from `power.draw` |
| An **idle** H100 still draws ~66 W | measured 63.8–70.4 W across the six idle cards |
| The box drew **1,532 W** with 2 of 8 cards busy | 6 idle ≈ 400 W, 2 busy ≈ 1,132 W → 36.8 kWh/day |

The second row is the one that kills the Docker option, and it is subtler than it looks:
the kernel does not merely fail to *resolve* a foreign UID, it rewrites the value to the
overflow UID before any lookup happens. A `uid -> username` table obtained from root would
therefore be handed `65534` and map it to `nobody`. See "Why not Docker" below.

The power rows motivate the idle rule in money rather than etiquette: six cards sitting at
~66 W is ~9.6 kWh/day of nothing.

## Global Constraints

- **Read-only. The tool MUST NOT signal, kill, deprioritise or otherwise touch any process.** It observes and reports. No code path may call `kill`, `pkill`, `nvidia-smi --gpu-reset`, or any `subprocess` invocation that is not a query.
- **It is a plain script on the host, run outside Docker.** Not containerised, not a package to install. See Measured facts and "Why not Docker".
- **Stdlib only.** No `requests`, no `pynvml`, no venv. A dependency-free single file tree is what makes this survivable as a background service on a shared box.
- **Energy is integrated from `power.draw`, never read from a counter** — this driver has no cumulative energy field. Every energy number the tool reports is `Σ power × Δt` over its own polls, so it measures only while the service is up. Reports must say so rather than implying a complete meter reading.
- **Energy attributed to a user is an estimate, and labelled as one.** `power.draw` is per *card*, not per process. With one process on a card the attribution is exact; with several the tool splits by memory share, which is a proxy, not a measurement.
- **Dry-run is the default.** `--post` must be passed explicitly before anything reaches Slack. The first deployment runs dry for at least a day.
- **Never send without passing the cooldown.** Every alert path goes through `state.should_send`; there is no direct call from a rule to the notifier.
- **The webhook URL is read from the environment or a mode-0600 file. It is never committed, never logged, never included in an error message.** Slack webhook URLs are bearer credentials.
- **Unknown owner is reported as unknown, never guessed.** If `/proc/<pid>` is gone or the UID does not resolve, the alert says so. Silently attributing to the wrong person is worse than saying "unattributed".
- GPU indices are integers 0–7. Policy keys are OS usernames exactly as `/proc` reports them (`dorian.zwanzig`, `cwinkelmann`).
- Timestamps are UTC, ISO-8601, `datetime.now(timezone.utc)`.

## File Structure

```
resourcemonitor/
  __init__.py
  model.py      Snapshot, GpuState, GpuProcess, Alert — frozen dataclasses, no I/O
  probe.py      nvidia-smi + /proc  -> Snapshot          (the only module that shells out)
  policy.py     TOML -> Policy, with validation
  rules.py      pure (Snapshot, Policy) -> list[Alert]
  energy.py     integrates power.draw between polls; kWh per GPU and per user
  state.py      incident identity + cooldown, JSON-backed
  notify.py     Alert -> Slack Block Kit payload -> webhook
  cli.py        `once`, `watch`, `report`, `--post`, `--dry-run`
tests/
  fixtures/     recorded nvidia-smi output, including the real 2026-10-06 situation
  test_probe.py test_policy.py test_rules_stateless.py test_rules_idle.py
  test_energy.py test_state.py test_notify.py test_cli.py test_skills_present.py
deploy/
  resourcemonitor.service
  policy.example.toml
.claude/skills/
  deploy-resourcemonitor/SKILL.md    install / update / troubleshoot on carrot
  gpu-energy-report/SKILL.md         per-user energy + occupancy, with its caveats
CLAUDE.md
README.md
```

`probe.py` is the only module that touches the system, so every other module is testable
on a laptop with no GPU. That boundary is the whole reason the layout looks like this.

---

### Task 1: Snapshot model and the nvidia-smi probe

**Files:**
- Create: `resourcemonitor/model.py`
- Create: `resourcemonitor/probe.py`
- Create: `tests/fixtures/nvidia_smi_2026_10_06.txt`
- Test: `tests/test_probe.py`

**Interfaces:**
- Produces: `GpuState(index: int, total_mib: int, used_mib: int, util_pct: int, power_w: float)`, `GpuProcess(pid: int, gpu_index: int, used_mib: int, user: str | None)`, `Snapshot(taken_at: datetime, gpus: tuple[GpuState, ...], procs: tuple[GpuProcess, ...])`, `Snapshot.free_mib(index) -> int`, `parse_gpu_query(text) -> tuple[GpuState, ...]`, `parse_apps_query(text, uuid_to_index) -> tuple[GpuProcess, ...]`, `probe() -> Snapshot`.
- `user` is `None` when the owner could not be resolved. Callers must render that as "unattributed".

- [ ] **Step 1: Record the fixture**

`nvidia-smi` joins processes to GPUs by **UUID, not index**, so the probe needs both
queries and a join. Record real output:

```bash
ssh cwinkelmann@10.188.1.1 'nvidia-smi --query-gpu=index,uuid,memory.total,memory.used,utilization.gpu --format=csv,noheader' \
  > tests/fixtures/gpu_query.txt
ssh cwinkelmann@10.188.1.1 'nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader' \
  > tests/fixtures/apps_query.txt
```

If the box is idle when you record, hand-write a second fixture reproducing the
2026-10-06 state: GPUs 0–5 at 0 MiB/0 %, GPU 6 at 22715 MiB/100 %, GPU 7 at 27367 MiB/98 %,
with PIDs 3078913 (22706 MiB, GPU 6) and 3079828 (27358 MiB, GPU 7). Later tasks assert
against those numbers.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_probe.py
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
```

- [ ] **Step 3: Run them and watch them fail**

Run: `python -m pytest tests/test_probe.py -v`
Expected: `ModuleNotFoundError: No module named 'resourcemonitor'`

- [ ] **Step 4: Implement model.py**

```python
"""Immutable view of what the GPUs are doing. No I/O lives here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GpuState:
    index: int
    total_mib: int
    used_mib: int
    util_pct: int
    power_w: float = 0.0      # 0.0 when the card does not report it ('[N/A]')


@dataclass(frozen=True)
class GpuProcess:
    pid: int
    gpu_index: int
    used_mib: int
    user: str | None = None      # None == could not resolve; render as "unattributed"


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    gpus: tuple[GpuState, ...]
    procs: tuple[GpuProcess, ...]

    def free_mib(self, index: int) -> int:
        for g in self.gpus:
            if g.index == index:
                return g.total_mib - g.used_mib
        raise KeyError(f"no GPU with index {index}")
```

- [ ] **Step 5: Implement probe.py**

```python
"""The only module that shells out. Everything else is pure."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot

GPU_FIELDS = "index,uuid,memory.total,memory.used,utilization.gpu,power.draw"
APP_FIELDS = "gpu_uuid,pid,used_memory"


def _num(cell: str) -> int:
    """'22715 MiB' -> 22715, '98 %' -> 98. nvidia-smi units break a bare int()."""
    return int(cell.strip().split()[0])


def _watts(cell: str) -> float:
    """'575.00 W' -> 575.0, '[N/A]' -> 0.0.

    Kept as a float: truncating 63.80 W loses 1.3% of an idle card, and the energy
    figures are a running sum, so that error compounds over a week.
    """
    try:
        return float(cell.strip().split()[0])
    except ValueError:
        return 0.0          # card does not report power; not a reason to fail the poll


def parse_gpu_query(text: str) -> tuple[GpuState, ...]:
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        index, _uuid, total, used, util, power = [c.strip() for c in line.split(",")]
        out.append(GpuState(int(index), _num(total), _num(used), _num(util), _watts(power)))
    return tuple(out)


def parse_gpu_uuids(text: str) -> dict[str, int]:
    out = {}
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        index, uuid, *_ = [c.strip() for c in line.split(",")]
        out[uuid] = int(index)
    return out


def parse_apps_query(text: str, uuid_to_index: dict[str, int]) -> tuple[GpuProcess, ...]:
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        uuid, pid, used = [c.strip() for c in line.split(",")]
        if uuid not in uuid_to_index:
            continue          # a GPU we did not enumerate; drop rather than guess
        out.append(GpuProcess(int(pid), uuid_to_index[uuid], _num(used)))
    return tuple(out)


def _run(fields: str, query: str) -> str:
    return subprocess.run(
        ["nvidia-smi", f"--query-{query}={fields}", "--format=csv,noheader"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


def probe() -> Snapshot:
    gpu_text = _run(GPU_FIELDS, "gpu")
    apps_text = _run(APP_FIELDS, "compute-apps")
    return Snapshot(
        taken_at=datetime.now(timezone.utc),
        gpus=parse_gpu_query(gpu_text),
        procs=parse_apps_query(apps_text, parse_gpu_uuids(gpu_text)),
    )
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_probe.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add resourcemonitor/model.py resourcemonitor/probe.py tests/test_probe.py tests/fixtures/
git commit -m "feat(probe): snapshot model and nvidia-smi parsing

Joins processes to GPUs by UUID, not by position: the compute-apps query
reports a gpu_uuid and zipping the two queries by index silently
mis-attributes every process on a box where the orders differ."
```

---

### Task 2: Owner attribution

**Files:**
- Modify: `resourcemonitor/probe.py`
- Test: `tests/test_owner.py`

**Interfaces:**
- Consumes: `GpuProcess` from Task 1.
- Produces: `owner_of(pid: int) -> str | None`, and `probe()` now fills `GpuProcess.user`.

This is the task the whole tool exists for, and the one the container cannot do. Read the
owner from `/proc/<pid>` rather than shelling out to `ps`: it is one `stat` call instead of
a process spawn per PID, and it cannot be confused by a username containing spaces.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_owner.py
import os
import pytest
from resourcemonitor.probe import owner_of


def test_resolves_the_owner_of_a_live_process():
    assert owner_of(os.getpid()) == __import__("getpass").getuser()


def test_a_dead_pid_returns_none_rather_than_raising():
    """Processes exit between the nvidia-smi call and the /proc read. That is normal."""
    assert owner_of(2 ** 22) is None


def test_an_unresolvable_uid_returns_none_not_a_number(monkeypatch):
    """Inside a rootless container every foreign UID maps to the overflow UID.

    The tool must say 'unattributed' there rather than inventing a name -- reporting
    the wrong person is worse than reporting nobody. See the plan's Measured facts:
    host uid=1053 dorian.zwanzig becomes uid=65534 nobody in a rootless container.
    """
    import resourcemonitor.probe as probe

    monkeypatch.setattr(probe, "_uid_of", lambda pid: 65534)
    monkeypatch.setattr(probe, "_name_of_uid", lambda uid: None)

    assert probe.owner_of(1234) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_owner.py -v`
Expected: FAIL, `cannot import name 'owner_of'`

- [ ] **Step 3: Implement**

Add to `probe.py`:

```python
import os
import pwd


def _uid_of(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None       # exited between the nvidia-smi call and this read


def _name_of_uid(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def owner_of(pid: int) -> str | None:
    """Username that owns `pid`, or None if it cannot be established.

    None is a real answer, not an error: the process may have exited, or this may be
    running somewhere the UID does not resolve. Callers render None as "unattributed"
    and never guess.
    """
    uid = _uid_of(pid)
    if uid is None:
        return None
    return _name_of_uid(uid)
```

and fill it in `probe()`:

```python
    procs = tuple(
        GpuProcess(p.pid, p.gpu_index, p.used_mib, owner_of(p.pid))
        for p in parse_apps_query(apps_text, parse_gpu_uuids(gpu_text))
    )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_owner.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Verify against the real box**

```bash
ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -c "
from resourcemonitor.probe import probe
s = probe()
for p in s.procs: print(p.pid, p.gpu_index, p.user, p.used_mib)
"'
```
Expected: a real username (e.g. `dorian.zwanzig`), never `nobody`, never `None`, for any
live process. If it prints `None` or `nobody`, you are running inside a container —
re-read the plan's Measured facts.

- [ ] **Step 6: Commit**

```bash
git add resourcemonitor/probe.py tests/test_owner.py
git commit -m "feat(probe): attribute processes to their owner via /proc

Reads st_uid from /proc/<pid> rather than spawning ps. Returns None rather
than guessing when the UID does not resolve, which is what happens to every
foreign UID inside a rootless container."
```

---

### Task 3: Policy configuration

**Files:**
- Create: `resourcemonitor/policy.py`
- Create: `deploy/policy.example.toml`
- Test: `tests/test_policy.py`

**Interfaces:**
- Produces: `Policy(assignments: dict[str, frozenset[int]], idle_util_pct: int, idle_min_mib: int, idle_grace_s: int, capacity_free_mib: int, cooldown_s: int, channel: str)`, `load_policy(path) -> Policy`.

- [ ] **Step 1: Write the example config**

```toml
# deploy/policy.example.toml
# Who may use which GPUs. Keys are OS usernames exactly as /proc reports them.
# Agreed 2026-08-19: dorian 0-3, cwinkelmann 4-7.
[assignments]
"dorian.zwanzig" = [0, 1, 2, 3]
"cwinkelmann"    = [4, 5, 6, 7]

[rules]
# A process holding at least idle_min_mib while the GPU sits below idle_util_pct
# for idle_grace_s is "parked" -- the forgotten-notebook case.
idle_util_pct   = 5
idle_min_mib    = 1024
idle_grace_s    = 1800      # 30 min: long enough not to flag a job between epochs

# Capacity alarm: fires when NO GPU has this much free. 40 GiB is the floor for
# starting a real training run on an 80 GB card.
capacity_free_mib = 40960

[notify]
cooldown_s = 3600           # one message per incident per hour, not per poll
channel    = "#gpu-watch"
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_policy.py
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
```

- [ ] **Step 3: Run them and watch them fail**

Run: `python -m pytest tests/test_policy.py -v`
Expected: FAIL, `No module named 'resourcemonitor.policy'`

- [ ] **Step 4: Implement**

```python
"""Policy is data, and bad policy is caught at load time, not at alert time."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

MAX_GPU_INDEX = 7


@dataclass(frozen=True)
class Policy:
    assignments: dict[str, frozenset[int]]
    idle_util_pct: int
    idle_min_mib: int
    idle_grace_s: int
    capacity_free_mib: int
    cooldown_s: int
    channel: str

    def owner_of_gpu(self, index: int) -> str | None:
        for user, gpus in self.assignments.items():
            if index in gpus:
                return user
        return None


def load_policy(path: Path | str) -> Policy:
    raw = tomllib.loads(Path(path).read_text())
    for section in ("assignments", "rules", "notify"):
        if section not in raw:
            raise ValueError(f"policy is missing the [{section}] section")

    assignments: dict[str, frozenset[int]] = {}
    seen: dict[int, str] = {}
    for user, gpus in raw["assignments"].items():
        for g in gpus:
            if not isinstance(g, int) or not 0 <= g <= MAX_GPU_INDEX:
                raise ValueError(f"{user}: {g} is not a GPU index in 0..{MAX_GPU_INDEX}")
            if g in seen:
                # Otherwise "is this user out of allocation" has two answers.
                raise ValueError(f"{seen[g]} and {user} are both assigned GPU {g}")
            seen[g] = user
        assignments[user] = frozenset(gpus)

    r, n = raw["rules"], raw["notify"]
    return Policy(
        assignments=assignments,
        idle_util_pct=int(r["idle_util_pct"]),
        idle_min_mib=int(r["idle_min_mib"]),
        idle_grace_s=int(r["idle_grace_s"]),
        capacity_free_mib=int(r["capacity_free_mib"]),
        cooldown_s=int(n["cooldown_s"]),
        channel=str(n["channel"]),
    )
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_policy.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add resourcemonitor/policy.py deploy/policy.example.toml tests/test_policy.py
git commit -m "feat(policy): validated TOML policy

Rejects overlapping assignments at load time: two users owning one GPU makes
'out of allocation' undecidable, and the failure would otherwise surface as a
wrong accusation."
```

---

### Task 4: The two stateless rules — allocation and capacity

**Files:**
- Create: `resourcemonitor/rules.py`
- Test: `tests/test_rules_stateless.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 1), `Policy` (Task 3).
- Produces: `Alert(kind: str, key: str, text: str, gpu_index: int | None, user: str | None)`, `check_allocation(snap, pol) -> list[Alert]`, `check_capacity(snap, pol) -> list[Alert]`.
- `Alert.key` is the incident identity used for cooldown in Task 6. It must be stable across polls of the same situation and different for different situations.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rules_stateless.py
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_rules_stateless.py -v`
Expected: FAIL, `No module named 'resourcemonitor.rules'`

- [ ] **Step 3: Implement**

```python
"""Rules are pure functions of a Snapshot. No I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass

from resourcemonitor.model import Snapshot
from resourcemonitor.policy import Policy


@dataclass(frozen=True)
class Alert:
    kind: str                 # "allocation" | "idle" | "capacity"
    key: str                  # incident identity; stable across polls
    text: str
    gpu_index: int | None = None
    user: str | None = None


def _mib(n: int) -> str:
    return f"{n / 1024:.1f} GiB"


def check_allocation(snap: Snapshot, pol: Policy) -> list[Alert]:
    """A process on a GPU assigned to somebody else."""
    out = []
    for p in snap.procs:
        if p.user is None:
            continue                       # unattributed: never accuse
        assignee = pol.owner_of_gpu(p.gpu_index)
        if assignee is None or assignee == p.user:
            continue
        out.append(Alert(
            kind="allocation",
            key=f"allocation:{p.user}:{p.gpu_index}",   # not the PID: one incident
            gpu_index=p.gpu_index,
            user=p.user,
            text=(f"{p.user} is using GPU {p.gpu_index} ({_mib(p.used_mib)}), "
                  f"which is assigned to {assignee}."),
        ))
    return out


def check_capacity(snap: Snapshot, pol: Policy) -> list[Alert]:
    """Nobody can start a job: no GPU has capacity_free_mib free."""
    free = {g.index: g.total_mib - g.used_mib for g in snap.gpus}
    if any(f >= pol.capacity_free_mib for f in free.values()):
        return []
    best = max(free, key=free.get) if free else None
    return [Alert(
        kind="capacity",
        key="capacity:all",
        text=(f"No GPU has {_mib(pol.capacity_free_mib)} free — the box is full. "
              f"Most free: GPU {best} with {_mib(free[best])}." if best is not None
              else "No GPUs reported."),
    )]
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_rules_stateless.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/rules.py tests/test_rules_stateless.py
git commit -m "feat(rules): allocation and capacity checks

Keys incidents by (user, gpu) rather than PID so a restarted job is the same
incident. Never alerts on a process whose owner could not be resolved -- a
wrong accusation is worse than a missed one."
```

---

### Task 5: The stateful rule — idle VRAM hold

**Files:**
- Modify: `resourcemonitor/rules.py`
- Test: `tests/test_rules_idle.py`

**Interfaces:**
- Produces: `IdleTracker` with `observe(snap, pol) -> list[Alert]`.

This rule cannot be a pure function of one snapshot: "idle for 30 minutes" needs history.
It is deliberately a separate object so the stateless rules stay trivially testable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rules_idle.py
from datetime import datetime, timedelta, timezone

from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.policy import Policy
from resourcemonitor.rules import IdleTracker

POL = Policy(assignments={}, idle_util_pct=5, idle_min_mib=1024, idle_grace_s=1800,
             capacity_free_mib=40960, cooldown_s=3600, channel="#c")
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _snap(t, util, mib=20000, user="someone", pid=42):
    return Snapshot(t, (GpuState(3, 81559, mib, util),),
                    (GpuProcess(pid, 3, mib, user),))


def test_silent_before_the_grace_period_elapses():
    tr = IdleTracker()
    assert tr.observe(_snap(T0, util=0), POL) == []
    assert tr.observe(_snap(T0 + timedelta(minutes=29), util=0), POL) == []


def test_fires_once_the_grace_period_elapses():
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)

    alerts = tr.observe(_snap(T0 + timedelta(minutes=31), util=0), POL)

    assert len(alerts) == 1
    assert alerts[0].kind == "idle"
    assert "30" in alerts[0].text or "31" in alerts[0].text


def test_activity_resets_the_timer():
    """A job between epochs dips to 0% briefly. That must not accumulate."""
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)
    tr.observe(_snap(T0 + timedelta(minutes=20), util=90), POL)      # busy again

    assert tr.observe(_snap(T0 + timedelta(minutes=35), util=0), POL) == []


def test_a_small_allocation_is_not_worth_flagging():
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0, mib=200), POL)

    assert tr.observe(_snap(T0 + timedelta(minutes=31), util=0, mib=200), POL) == []


def test_a_vanished_process_is_forgotten():
    """Otherwise the tracker leaks an entry per PID that ever ran."""
    tr = IdleTracker()
    tr.observe(_snap(T0, util=0), POL)
    empty = Snapshot(T0 + timedelta(minutes=5), (GpuState(3, 81559, 0, 0),), ())

    tr.observe(empty, POL)

    assert tr.tracked() == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_rules_idle.py -v`
Expected: FAIL, `cannot import name 'IdleTracker'`

- [ ] **Step 3: Implement**

Add to `rules.py`:

```python
class IdleTracker:
    """Remembers when each process last looked busy.

    GPU utilisation is per-DEVICE, not per-process, so "idle" here means the card
    this process sits on is below the threshold. With one process per card that is
    exact; with two it is conservative, which is the right direction -- it under-reports
    rather than accusing a busy job of being parked.
    """

    def __init__(self) -> None:
        self._busy_since: dict[int, object] = {}   # pid -> last time it looked busy

    def tracked(self) -> int:
        return len(self._busy_since)

    def observe(self, snap: Snapshot, pol: Policy) -> list[Alert]:
        util = {g.index: g.util_pct for g in snap.gpus}
        live = {p.pid for p in snap.procs}
        for pid in list(self._busy_since):
            if pid not in live:
                del self._busy_since[pid]          # exited; forget it

        out = []
        for p in snap.procs:
            if p.used_mib < pol.idle_min_mib:
                self._busy_since.pop(p.pid, None)
                continue
            if util.get(p.gpu_index, 100) > pol.idle_util_pct:
                self._busy_since[p.pid] = snap.taken_at
                continue
            first = self._busy_since.setdefault(p.pid, snap.taken_at)
            idle_s = (snap.taken_at - first).total_seconds()
            if idle_s >= pol.idle_grace_s:
                out.append(Alert(
                    kind="idle",
                    key=f"idle:{p.user}:{p.gpu_index}:{p.pid}",
                    gpu_index=p.gpu_index,
                    user=p.user,
                    text=(f"{p.user or 'an unattributed process'} has held "
                          f"{_mib(p.used_mib)} on GPU {p.gpu_index} at "
                          f"{util.get(p.gpu_index, 0)}% utilisation for "
                          f"{int(idle_s // 60)} minutes."),
                ))
        return out
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_rules_idle.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/rules.py tests/test_rules_idle.py
git commit -m "feat(rules): idle VRAM-hold detection with a grace period

Utilisation is per-device, so with two processes on one card this
under-reports rather than accusing a busy job of being parked. Activity
resets the timer so a job between epochs is not flagged."
```

---

### Task 6: Electricity — energy accumulation and per-user attribution

**Files:**
- Create: `resourcemonitor/energy.py`
- Test: `tests/test_energy.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 1, now carrying `power_w`).
- Produces: `EnergyLedger.load(path)`, `.accumulate(snap) -> None`, `.totals() -> dict`, `.save()`, `format_report(totals, price_per_kwh) -> str`.

This driver exposes no cumulative energy counter, so energy is `Σ power × Δt` over the
tool's own polls. Two consequences the implementation must honour: the ledger only knows
about time the service was running, and a long gap between polls (a restart, a suspended
box) must not be integrated as if the last reading held throughout.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_energy.py
from datetime import datetime, timedelta, timezone

from resourcemonitor.energy import EnergyLedger, format_report
from resourcemonitor.model import GpuProcess, GpuState, Snapshot

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _snap(t, gpus, procs=()):
    return Snapshot(t, tuple(gpus), tuple(procs))


def test_first_snapshot_accumulates_nothing():
    """There is no interval yet; integrating from nothing invents energy."""
    led = EnergyLedger()
    led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 0, 700.0)]))

    assert led.totals()["per_gpu"].get(0, 0.0) == 0.0


def test_one_hour_at_700w_is_0_7_kwh():
    led = EnergyLedger()
    led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 100, 700.0)]))
    led.accumulate(_snap(T0 + timedelta(hours=1), [GpuState(0, 81559, 0, 100, 700.0)]))

    assert round(led.totals()["per_gpu"][0], 3) == 0.700


def test_energy_follows_the_user_holding_the_card():
    led = EnergyLedger()
    g = [GpuState(6, 81559, 22715, 100, 600.0)]
    p = [GpuProcess(1, 6, 22715, "dorian.zwanzig")]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert round(led.totals()["per_user"]["dorian.zwanzig"], 3) == 0.600


def test_two_users_on_one_card_split_by_memory_share():
    """power.draw is per CARD. Splitting by memory is a proxy and is labelled as one."""
    led = EnergyLedger()
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 20000, "a"), GpuProcess(2, 6, 10000, "b")]
    led.accumulate(_snap(T0, g, p))
    led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    tot = led.totals()["per_user"]
    assert round(tot["a"], 2) == 0.40
    assert round(tot["b"], 2) == 0.20


def test_idle_energy_is_attributed_to_nobody_not_dropped():
    """An idle card still burns ~66 W. That is the number the idle rule exists for."""
    led = EnergyLedger()
    g = [GpuState(0, 81559, 0, 0, 66.0)]
    led.accumulate(_snap(T0, g))
    led.accumulate(_snap(T0 + timedelta(hours=1), g))

    t = led.totals()
    assert round(t["idle_kwh"], 3) == 0.066
    assert t["per_user"] == {}


def test_a_long_gap_is_not_integrated():
    """After a restart the last reading is stale; integrating it invents kWh."""
    led = EnergyLedger(max_gap_s=300)
    g = [GpuState(0, 81559, 0, 100, 700.0)]
    led.accumulate(_snap(T0, g))
    led.accumulate(_snap(T0 + timedelta(hours=6), g))

    assert led.totals()["per_gpu"].get(0, 0.0) == 0.0


def test_report_states_that_it_measures_only_uptime():
    txt = format_report({"per_gpu": {0: 1.0}, "per_user": {"a": 1.0},
                         "idle_kwh": 0.5, "since": T0.isoformat()}, price_per_kwh=0.30)

    assert "0.30" in txt or "€" in txt
    assert "while the monitor was running" in txt.lower() or "uptime" in txt.lower()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_energy.py -v`
Expected: FAIL, `No module named 'resourcemonitor.energy'`

- [ ] **Step 3: Implement**

```python
"""Energy = sum(power x dt) over our own polls. This driver has no energy counter.

Everything here is an estimate with a stated shape:
  * per GPU   -- as good as nvidia-smi's power.draw and the poll interval
  * per user  -- power is per CARD, so several processes on one card are split by
                 memory share. That is a proxy, not a measurement, and the report says so.
  * idle      -- power drawn by a card with no compute process on it. Nobody is billed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class EnergyLedger:
    max_gap_s: int = 300
    per_gpu_kwh: dict[int, float] = field(default_factory=dict)
    per_user_kwh: dict[str, float] = field(default_factory=dict)
    idle_kwh: float = 0.0
    since: str | None = None
    _last: Snapshot | None = field(default=None, repr=False)  # type: ignore[name-defined]

    def accumulate(self, snap) -> None:
        prev, self._last = self._last, snap
        if self.since is None:
            self.since = snap.taken_at.isoformat()
        if prev is None:
            return                                  # no interval yet
        dt_s = (snap.taken_at - prev.taken_at).total_seconds()
        if dt_s <= 0 or dt_s > self.max_gap_s:
            return                                  # restart or clock jump: do not invent

        hours = dt_s / 3600.0
        procs_by_gpu: dict[int, list] = {}
        for p in prev.procs:
            procs_by_gpu.setdefault(p.gpu_index, []).append(p)

        for g in prev.gpus:
            kwh = g.power_w * hours / 1000.0
            self.per_gpu_kwh[g.index] = self.per_gpu_kwh.get(g.index, 0.0) + kwh
            holders = procs_by_gpu.get(g.index, [])
            if not holders:
                self.idle_kwh += kwh
                continue
            total_mib = sum(max(p.used_mib, 1) for p in holders)
            for p in holders:
                if p.user is None:
                    continue                        # unattributed: do not bill a guess
                share = max(p.used_mib, 1) / total_mib
                self.per_user_kwh[p.user] = self.per_user_kwh.get(p.user, 0.0) + kwh * share

    def totals(self) -> dict:
        return {"per_gpu": dict(self.per_gpu_kwh), "per_user": dict(self.per_user_kwh),
                "idle_kwh": self.idle_kwh, "since": self.since}

    @classmethod
    def load(cls, path: Path | str, max_gap_s: int = 300) -> "EnergyLedger":
        try:
            d = json.loads(Path(path).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls(max_gap_s=max_gap_s)
        led = cls(max_gap_s=max_gap_s, idle_kwh=d.get("idle_kwh", 0.0),
                  since=d.get("since"))
        led.per_gpu_kwh = {int(k): v for k, v in d.get("per_gpu", {}).items()}
        led.per_user_kwh = dict(d.get("per_user", {}))
        return led

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.totals(), indent=1))
        tmp.replace(path)


def format_report(totals: dict, price_per_kwh: float) -> str:
    users = sorted(totals["per_user"].items(), key=lambda kv: -kv[1])
    lines = [f"*GPU energy since {totals.get('since', '?')}*",
             "_Measured only while the monitor was running; this driver exposes no "
             "cumulative counter._", ""]
    for user, kwh in users:
        lines.append(f"• {user}: {kwh:.1f} kWh  (~€{kwh * price_per_kwh:.2f})")
    idle = totals["idle_kwh"]
    lines.append(f"• _idle cards (nobody): {idle:.1f} kWh (~€{idle * price_per_kwh:.2f})_")
    lines.append("")
    lines.append("Per-user figures split a card's draw by memory share when several "
                 "processes share it — an estimate, not a measurement.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_energy.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/energy.py tests/test_energy.py
git commit -m "feat(energy): integrate power.draw into per-user kWh

This driver has no total_energy_consumption field, so energy is summed over
our own polls. Refuses to integrate across a gap longer than max_gap_s: after
a restart the previous reading is stale and integrating it invents kWh."
```

---

### Task 7: Cooldown and incident state

**Files:**
- Create: `resourcemonitor/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `State.load(path)`, `State.should_send(key, now, cooldown_s) -> bool`, `State.save()`.

Without this the tool posts the same message every poll cycle — a minute apart — and gets
muted within the hour. Nothing may reach Slack without passing through here.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
from datetime import datetime, timedelta, timezone

from resourcemonitor.state import State

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def test_first_sighting_sends(tmp_path):
    st = State.load(tmp_path / "s.json")

    assert st.should_send("allocation:dorian:6", T0, 3600) is True


def test_same_incident_is_suppressed_within_the_cooldown(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("k", T0, 3600)

    assert st.should_send("k", T0 + timedelta(minutes=59), 3600) is False


def test_it_sends_again_once_the_cooldown_expires(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("k", T0, 3600)

    assert st.should_send("k", T0 + timedelta(minutes=61), 3600) is True


def test_distinct_incidents_do_not_suppress_each_other(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.should_send("allocation:dorian:6", T0, 3600)

    assert st.should_send("allocation:dorian:7", T0, 3600) is True


def test_state_survives_a_restart(tmp_path):
    """systemd restarts the service; a forgotten cooldown means a duplicate alert."""
    p = tmp_path / "s.json"
    st = State.load(p)
    st.should_send("k", T0, 3600)
    st.save()

    assert State.load(p).should_send("k", T0 + timedelta(minutes=5), 3600) is False


def test_a_corrupt_state_file_does_not_crash_the_daemon(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")

    assert State.load(p).should_send("k", T0, 3600) is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL, `No module named 'resourcemonitor.state'`

- [ ] **Step 3: Implement**

```python
"""One message per incident per cooldown, across restarts."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class State:
    def __init__(self, path: Path, sent: dict[str, str]) -> None:
        self._path = path
        self._sent = sent

    @classmethod
    def load(cls, path: Path | str) -> "State":
        path = Path(path)
        try:
            return cls(path, json.loads(path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            # A corrupt file must not take the daemon down; the cost is at most one
            # duplicate alert, which is far cheaper than a monitor that is not running.
            return cls(path, {})

    def should_send(self, key: str, now: datetime, cooldown_s: int) -> bool:
        last = self._sent.get(key)
        if last is not None:
            if (now - datetime.fromisoformat(last)).total_seconds() < cooldown_s:
                return False
        self._sent[key] = now.isoformat()
        return True

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._sent, indent=1))
        tmp.replace(self._path)            # atomic; a killed daemon cannot truncate it
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/state.py tests/test_state.py
git commit -m "feat(state): per-incident cooldown that survives restarts

Writes atomically via a temp file: a daemon killed mid-write would otherwise
leave truncated JSON and re-alert everything on the next start."
```

---

### Task 8: Slack notifier

**Files:**
- Create: `resourcemonitor/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `build_payload(alerts, host) -> dict`, `Notifier(webhook_url, dry_run).send(alerts, host) -> bool`.
- Consumes: `Alert` (Task 4).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notify.py
import json
import pytest

from resourcemonitor.notify import Notifier, build_payload
from resourcemonitor.rules import Alert

A = Alert(kind="allocation", key="allocation:dorian.zwanzig:6", gpu_index=6,
          user="dorian.zwanzig",
          text="dorian.zwanzig is using GPU 6 (22.2 GiB), which is assigned to cwinkelmann.")


def test_payload_mentions_the_user_so_slack_notifies_them():
    body = json.dumps(build_payload([A], host="carrot"))

    assert "dorian.zwanzig" in body
    assert "carrot" in body


def test_payload_groups_several_alerts_into_one_message():
    """Two violations are one notification, not two pings."""
    payload = build_payload([A, A], host="carrot")

    assert json.dumps(payload).count("GPU 6") == 2
    assert isinstance(payload["blocks"], list)


def test_dry_run_never_opens_the_network(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry run must not call urlopen")

    monkeypatch.setattr("urllib.request.urlopen", explode)

    assert Notifier("https://hooks.slack.test/x", dry_run=True).send([A], "carrot") is True


def test_the_webhook_url_never_appears_in_the_repr():
    """It is a bearer credential; a traceback or log line must not leak it."""
    n = Notifier("https://hooks.slack.com/services/T/B/SECRET", dry_run=True)

    assert "SECRET" not in repr(n)


def test_empty_alert_list_sends_nothing(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))

    assert Notifier("https://x", dry_run=False).send([], "carrot") is False
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_notify.py -v`
Expected: FAIL, `No module named 'resourcemonitor.notify'`

- [ ] **Step 3: Implement**

```python
"""Slack incoming webhook. One grouped message per cycle."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from resourcemonitor.rules import Alert

ICON = {"allocation": ":no_entry_sign:", "idle": ":zzz:", "capacity": ":rotating_light:"}


def build_payload(alerts: list[Alert], host: str) -> dict:
    lines = [f"{ICON.get(a.kind, ':grey_question:')} {a.text}" for a in alerts]
    return {
        "text": f"GPU report for {host}",     # fallback for notifications
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"GPU report — {host}"}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }


class Notifier:
    def __init__(self, webhook_url: str, dry_run: bool = True) -> None:
        self._url = webhook_url
        self.dry_run = dry_run

    def __repr__(self) -> str:        # never leak the URL into logs or tracebacks
        return f"Notifier(dry_run={self.dry_run})"

    def send(self, alerts: list[Alert], host: str) -> bool:
        if not alerts:
            return False
        payload = build_payload(alerts, host)
        if self.dry_run:
            print(json.dumps(payload, indent=2))
            return True
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except urllib.error.URLError as e:
            # Slack being down must not kill the daemon. Report and carry on; the
            # cooldown has already been consumed, so this incident waits a cycle.
            print(f"slack post failed: {e.__class__.__name__}")
            return False
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_notify.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/notify.py tests/test_notify.py
git commit -m "feat(notify): Slack webhook with a dry-run default

repr() omits the URL: a webhook is a bearer credential and a traceback in a
service log is a plausible way to leak one."
```

---

### Task 9: CLI, systemd unit and README

**Files:**
- Create: `resourcemonitor/cli.py`
- Create: `deploy/resourcemonitor.service`
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `python -m resourcemonitor once|watch [--post] [--policy P] [--state S]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL, `No module named 'resourcemonitor.cli'`

- [ ] **Step 3: Implement the CLI**

```python
"""`once` for a single pass, `watch` for the service. Dry-run unless --post."""
from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path

from resourcemonitor.energy import EnergyLedger, format_report
from resourcemonitor.notify import Notifier
from resourcemonitor.policy import load_policy
from resourcemonitor.probe import probe
from resourcemonitor.rules import Alert, IdleTracker, check_allocation, check_capacity
from resourcemonitor.state import State

DEFAULT_POLICY = Path.home() / ".config/resourcemonitor/policy.toml"
DEFAULT_STATE = Path.home() / ".local/state/resourcemonitor/state.json"
DEFAULT_ENERGY = Path.home() / ".local/state/resourcemonitor/energy.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resourcemonitor")
    p.add_argument("mode", choices=["once", "watch", "report"])
    p.add_argument("--post", action="store_true",
                   help="actually post to Slack (default: print the payload only)")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--energy", type=Path, default=DEFAULT_ENERGY)
    p.add_argument("--price", type=float, default=0.30,
                   help="EUR per kWh, used only to annotate the report")
    return p


def run_once(pol, state, notifier, tracker, ledger, host, energy_path) -> int:
    snap = probe()
    ledger.accumulate(snap)          # before the rules: a poll always costs energy
    ledger.save(energy_path)
    alerts = check_allocation(snap, pol) + check_capacity(snap, pol) \
        + tracker.observe(snap, pol)
    fresh = [a for a in alerts if state.should_send(a.key, snap.taken_at, pol.cooldown_s)]
    if fresh:
        notifier.send(fresh, host)
    state.save()
    return len(fresh)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    pol = load_policy(args.policy)
    state = State.load(args.state)
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if args.post and not url:
        raise SystemExit("--post given but SLACK_WEBHOOK_URL is not set")
    notifier = Notifier(url, dry_run=not args.post)
    tracker = IdleTracker()
    ledger = EnergyLedger.load(args.energy, max_gap_s=args.interval * 5)
    host = socket.gethostname()

    if args.mode == "report":
        # Reads the ledger only; it does not poll, so it is safe to run any time.
        text = format_report(EnergyLedger.load(args.energy).totals(), args.price)
        if args.post:
            notifier.send([Alert(kind="report", key="report:manual", text=text)], host)
        else:
            print(text)
        return 0

    if args.mode == "once":
        run_once(pol, state, notifier, tracker, ledger, host, args.energy)
        return 0

    while True:                      # watch
        try:
            run_once(pol, state, notifier, tracker, ledger, host, args.energy)
        except Exception as e:       # a bad poll must not end the service
            print(f"poll failed: {e.__class__.__name__}: {e}")
        time.sleep(args.interval)
```

- [ ] **Step 4: Write the systemd unit**

```ini
# deploy/resourcemonitor.service  ->  ~/.config/systemd/user/
[Unit]
Description=GPU resource monitor (read-only; posts to Slack)
After=network-online.target

[Service]
Type=simple
# Linger=yes is already set for this user, so the service survives logout.
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=%h/.config/resourcemonitor/env     # mode 0600; holds SLACK_WEBHOOK_URL
ExecStart=/usr/bin/python3 -m resourcemonitor watch --interval 60 --post
WorkingDirectory=%h/ResourceMonitor
Restart=always
RestartSec=30
# It only ever reads. Make that structural, not just a convention.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=default.target
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/ -v`
Expected: PASS (all suites)

- [ ] **Step 6: Deploy dry and watch it for a day**

```bash
rsync -a --exclude .git ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/
ssh cwinkelmann@10.188.1.1 'mkdir -p ~/.config/resourcemonitor && \
  cp ~/ResourceMonitor/deploy/policy.example.toml ~/.config/resourcemonitor/policy.toml'
# one pass, printing the payload instead of sending it
ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -m resourcemonitor once'
```
Expected on the 2026-10-06 state: two `allocation` alerts naming `dorian.zwanzig` on GPUs
6 and 7, no `capacity` alert (GPUs 0–5 are empty), no `idle` alert (both at ~100 %).

Leave it running **without** `--post` for a day before enabling the unit. Read the printed
payloads and confirm the alert volume is what you would have wanted to receive.

- [ ] **Step 7: Write the README**

Cover: what it does, the three rules in one line each, the measured-facts table (especially
why it cannot run in a rootless container), how to set the webhook, how to dry-run, and an
explicit statement that the tool never kills anything.

- [ ] **Step 8: Commit**

```bash
git add resourcemonitor/cli.py deploy/resourcemonitor.service README.md tests/test_cli.py
git commit -m "feat(cli): once/watch modes, systemd unit, docs

Posting is opt-in: a misconfigured first start prints payloads rather than
spamming a channel. The unit hardens the service as read-only at the systemd
level, so 'it only observes' is structural rather than a convention."
```

---

### Task 10: CLAUDE.md and the two project skills

**Files:**
- Create: `CLAUDE.md`
- Create: `.claude/skills/deploy-resourcemonitor/SKILL.md`
- Create: `.claude/skills/gpu-energy-report/SKILL.md`
- Test: `tests/test_skills_present.py`

**Interfaces:**
- Consumes: the CLI from Task 9.
- Project skills live in `.claude/skills/<name>/SKILL.md`. **Never `.superpowers/skills/`** — that path is not loaded.

Deployment and report generation are the two things that will be done repeatedly, by
someone who has forgotten the details. Both are mechanical and both have a step that is
easy to get wrong (the webhook file's mode; the fact that the report measures uptime only),
so they belong in skills rather than in a human's memory.

- [ ] **Step 1: Write CLAUDE.md**

```markdown
# CLAUDE.md — ResourceMonitor

A read-only watcher for the shared GPU box. It attributes GPU occupancy and electricity
to users and posts to Slack.

## The box

`ssh cwinkelmann@10.188.1.1` — **the name "carrot" does not resolve in DNS, use the IP.**
Key-based auth, no password. 8 × H100 80GB HBM3, shared with `dorian.zwanzig` (UID 1053);
the split is dorian 0–3, cwinkelmann 4–7.

Run it with `/usr/bin/python3` (3.12.3). conda is deliberately not used and is not on
`PATH` over non-interactive ssh — the tool is stdlib-only so it keeps working when envs
come and go.

## Non-negotiables

- **It never kills, signals or reprioritises anything.** Observation only. If a change
  would add a write path to another user's process, it does not belong here.
- **It runs on the host, outside Docker.** A rootless container rewrites every foreign
  UID to 65534, so attribution silently becomes `nobody`. Verified 2026-10-06; see the
  plan's Measured facts. Do not "containerise it for consistency".
- **Stdlib only.** No requests, no pynvml, no venv — it must survive a shared box with
  no maintenance.
- **Dry-run is the default.** `--post` is opt-in.
- **The Slack webhook is a bearer credential.** It lives in `~/.config/resourcemonitor/env`
  at mode 0600, is read from the environment, and never appears in a log, a repr or a
  traceback.

## Layout

`probe.py` is the only module that shells out. Everything else is a pure function of a
`Snapshot`, which is why the rules are testable on a laptop with no GPU. Keep that seam.

## Energy numbers are estimates

This driver has no `total_energy_consumption` counter, so energy is `Σ power × Δt` over
the tool's own polls: it measures **only while the service was running**. `power.draw` is
per card, so when several processes share a GPU the split is by memory share — a proxy.
Any report that states a kWh figure must also state both caveats.

## The GPU split

dorian 0–3, cwinkelmann 4–7 (agreed 2026-08-19). It lives in
`~/.config/resourcemonitor/policy.toml`, not in code.

## Skills

- `deploy-resourcemonitor` — install or update the service on carrot
- `gpu-energy-report` — produce a per-user energy and occupancy report
```

- [ ] **Step 2: Write the deployment skill**

```markdown
---
name: deploy-resourcemonitor
description: Use when installing, updating or restarting the GPU ResourceMonitor service on carrot, or when its Slack alerts have stopped arriving. Covers the host-only constraint, the webhook secret, the dry-run soak and the systemd user unit.
---

# Deploying ResourceMonitor

## Reaching the box

| | |
|---|---|
| address | **`cwinkelmann@10.188.1.1`** |
| hostname | **`carrot` does not resolve in DNS** (`SERVFAIL`) — use the IP, always |
| auth | key-based; `ssh -o BatchMode=yes` works with no prompt |
| python | `/usr/bin/python3` (3.12.3). **conda is not on `PATH`** over non-interactive ssh — which is what we want, since the tool is stdlib-only. |
| hardware | 8 × H100 80GB HBM3, driver 580.178.04, shared with `dorian.zwanzig` |

Quick liveness check before anything else:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 'hostname; nvidia-smi --query-gpu=count --format=csv,noheader | head -1'
```

The service runs as a `systemd --user` unit; `Linger=yes` is already set for this user, so
it survives logout and needs no root.

## Never containerise this

A rootless container rewrites every foreign UID to 65534 (`nobody`), so process
attribution silently breaks while the tool appears to work. Measured 2026-10-06:
`/proc/<pid>/status` reads `Uid: 1053` on the host and `Uid: 65534` in the container.
Asking root for a uid→name table does not help — the UID is destroyed before any lookup.

## Steps

1. **Sync the code**

   ```bash
   rsync -a --exclude .git --exclude __pycache__ ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/
   ```

2. **Policy** — `~/.config/resourcemonitor/policy.toml`, from `deploy/policy.example.toml`.
   The `[assignments]` keys are OS usernames exactly as `/proc` reports them.

3. **Webhook secret** — mode 0600, never committed:

   ```bash
   ssh cwinkelmann@10.188.1.1 'install -m 600 /dev/null ~/.config/resourcemonitor/env && \
     printf "SLACK_WEBHOOK_URL=%s\n" "$URL" > ~/.config/resourcemonitor/env'
   ```

4. **Dry run first, and read the output.**

   ```bash
   ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -m resourcemonitor once'
   ```

   It prints the Slack payload instead of sending it. Confirm the alerts are ones you
   would have wanted to receive. Leave it dry for a day before step 5 — the cost of a
   noisy first week is that the channel gets muted and the tool becomes useless.

5. **Enable the unit**

   ```bash
   ssh cwinkelmann@10.188.1.1 'cp ~/ResourceMonitor/deploy/resourcemonitor.service \
     ~/.config/systemd/user/ && systemctl --user daemon-reload && \
     systemctl --user enable --now resourcemonitor'
   ```

6. **Verify**

   ```bash
   ssh cwinkelmann@10.188.1.1 'systemctl --user status resourcemonitor --no-pager | head -20'
   ```

## When alerts stop arriving

In this order: `systemctl --user status` (is it running?); `journalctl --user -u
resourcemonitor -n 50` (is it erroring?); check the state file — an incident inside its
cooldown is *supposed* to be silent; confirm `SLACK_WEBHOOK_URL` is still set, since an
expired webhook returns a non-2xx that the tool logs but does not crash on.
```

- [ ] **Step 3: Write the report skill**

```markdown
---
name: gpu-energy-report
description: Use when asked how much electricity or GPU time the shared box or a particular person has used, to produce a weekly energy report, or to explain why a kWh figure looks lower than expected. Covers the uptime-only caveat and the per-card attribution proxy.
---

# GPU energy and occupancy reports

```bash
ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -m resourcemonitor report'
```

Add `--post` to send it to Slack instead of printing it.

## Two caveats that must travel with every number

1. **It measures uptime, not history.** The driver on carrot exposes no
   `total_energy_consumption` counter, so energy is `Σ power × Δt` over the monitor's own
   polls. Any period when the service was down is simply missing — the total is a floor,
   not a meter reading. If a figure looks low, check `systemctl --user status` before
   concluding usage was low.
2. **Per-user figures are an estimate.** `power.draw` is per *card*. One process on a card
   gives exact attribution; several are split by memory share, which is a proxy for
   compute, not a measurement. Say so when quoting a per-person number.

## Reference points (measured 2026-10-06)

- An **idle** H100 still draws ~66 W, so six idle cards ≈ 400 W ≈ 9.6 kWh/day.
- A busy card drew 557–575 W against a 700 W limit.
- The box totalled 1,532 W with 2 of 8 cards busy ≈ 36.8 kWh/day.

Idle draw is attributed to nobody and reported on its own line. It is usually the most
actionable number in the report: it is the cost of cards nobody is using.

## Sanity checks before sending a report

- Does `per_gpu` sum to roughly `per_user + idle`? A large gap means unattributed
  processes — check for `None` owners.
- Is `since` when you think the service started? If it is more recent, it restarted.
```

- [ ] **Step 4: Write the failing test**

```python
# tests/test_skills_present.py
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
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_skills_present.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md .claude/skills tests/test_skills_present.py
git commit -m "docs: CLAUDE.md and the deploy/report skills

Both skills carry the caveats that are easy to forget and expensive to get
wrong: deployment must not be containerised, and every kWh figure measures
uptime only and splits a shared card by memory share."
```

---

## Why not Docker

Asked and measured on 2026-10-06, because running it in a container is the obvious
instinct.

A rootless container **can** see host PIDs — `docker run --pid=host` showed
`/proc/3078913` fine. What it cannot do is say who owns them:

| | host | rootless container, `--pid=host` |
|---|---|---|
| `stat /proc/<pid>` | `uid=1053 name=dorian.zwanzig` | `uid=65534 name=nobody` |
| `/proc/<pid>/status` | `Uid: 1053` | `Uid: 65534` |

The second row is the important one. The kernel does not fail to *resolve* the UID — it
**rewrites the value** to the overflow UID, because 1053 falls outside `cwinkelmann`'s
subuid range (`951968:65536`). So obtaining a `uid -> username` table from root does not
help: the table would be handed 65534.

What would actually be required, and why neither is worth it:

- **Root extends `/etc/subuid` to cover real user UIDs.** This would let this user's
  containers *act as* those UIDs — i.e. impersonate colleagues. Root should refuse, and we
  should not ask.
- **A rootful daemon, or `--userns=host`.** Needs root to grant docker access, which is a
  larger privilege than the problem justifies for a script that reads `nvidia-smi`.

Running it on the host needs none of that: `Linger=yes` is already set, so a
`systemd --user` unit survives logout with no admin involvement at all. That is why this
is specified as a plain script rather than an image.

---

## Deferred (explicitly not in v1)

- **Direct messages to offenders.** Needs a `username -> Slack member ID` map and a bot token. The channel post with an `@mention` is the agreed v1; revisit once the alert volume is known to be sane.
- **Telegram backend.** The `Notifier` interface is small enough to grow a second implementation if Slack proves wrong.
- **Quiet hours.** Add only if the real alert volume turns out to be nocturnal.
- **Multi-host.** `probe()` is local-only. Watching the t14 as well means either a second instance posting to the same channel, or an SSH-based probe — the former is simpler and has no credential story.
- **Reserving/queueing GPUs.** Out of scope. This tool reports; it never arbitrates.
