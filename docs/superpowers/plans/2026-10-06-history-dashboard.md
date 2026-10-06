# History + LAN Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every poll into a SQLite history and serve a read-only, unauthenticated LAN dashboard on carrot. It shows **who does what when**: who is on which GPU now and what they're running, a per-GPU timeline of jobs, and kWh and GPU-hours per user over time.

**Architecture:** The existing `watch` service appends one transaction per poll to `history.sqlite` (WAL mode). A separate `serve` process, running in its own systemd user unit, opens that file **read-only** (`mode=ro`). It serves one static HTML/JS/CSS page plus a small JSON API on stdlib `ThreadingHTTPServer`. The web process never imports `probe` or `notify`.

**Tech Stack:** Python 3.12 stdlib only (`sqlite3`, `http.server`, `json`, `datetime`). Vanilla JS with inline SVG charts. No CDN, no JS libraries, no build step.

**Spec:** none as a file. Design sections 1 (data model) and 2 (web/API) were approved in chat on 2026-10-06. Section 3 (page) and section 4 (testing/deploy) are decided in this plan, because the user said "just implement it". Builds on v1: `docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md` (its Global Constraints still bind).

## Global Constraints

- **Stdlib only.** No third-party Python packages, no JS libraries, no external URLs in the page (fonts, CDNs, images all forbidden).
- **Read-only toward processes.** Nothing new may signal, kill or reprioritise anything. Unchanged from v1.
- **The web process never imports `resourcemonitor.probe` or `resourcemonitor.notify`.** It opens SQLite with `file:<path>?mode=ro` (URI) and cannot write.
- **History writing must never break monitoring.** Any `sqlite3.Error` or `OSError` in the history path is caught in `run_once`. It is printed as `history write failed: <ExceptionClassName>` (class name only), and alerts and energy carry on.
- **Energy rows are written, not recomputed.** History `energy_samples` come from exactly the breakdown `EnergyLedger.accumulate()` produces. The same memory-share split applies; idle and unattributed go to their own buckets.
- **Reserved bucket names:** `"(idle)"` and `"(unattributed)"`. Parentheses cannot occur in Linux usernames, so these can never collide with a user.
- **Timestamps:** UTC ISO-8601 strings from `snap.taken_at.isoformat()`. Day and week grouping uses **UTC days**, and ISO weeks start Monday. The page labels this "UTC".
- **No auth.** This was the user's explicit choice; the page is visible to the LAN. Bind defaults to `127.0.0.1`; the deployed unit passes `--bind 10.188.1.1 --port 8765`.
- **Fixed routes only.** `/`, `/app.js`, `/app.css`, `/favicon.svg`, `/api/now`, `/api/usage`, `/api/timeseries`, `/api/vram`, `/api/timeline`, `/healthz`, `/api/claims` (GET, POST), `/api/claims/<id>/cancel` (POST), `/api/claims/quick` (POST), `/api/users` (GET), plus `/favicon.ico` answered with 204 No Content. Otherwise GET only: any other method gets 405, any other path gets 404. Request input is never used as a file path.
- **Response headers on every response:** `Content-Security-Policy: default-src 'self'` and `X-Content-Type-Options: nosniff`. No CORS headers. Because of the CSP, **no inline `<script>` or `<style>`** may appear in index.html.
- **Bad query parameters get a 400** with body `{"error": "bad request"}`, never a traceback. Ranges are capped: `/api/usage` needs `from <= to` and spans at most 366 days; `/api/timeseries` `hours` must be in 1..168.
- **No per-request access logging.** Override `log_message` to stay silent. Errors are printed by exception class only.
- Energy caveats travel with every kWh figure. `/api/usage` returns them, and the page shows them: *"Measured only while the monitor was running."* and *"Per-user kWh splits a card's draw by memory share when several processes share it — an estimate, not a measurement."*
- **Commits carry NO `Co-Authored-By` trailer or any other AI-attribution line.** This is the user's instruction and overrides any default.

## Review Focus

1. **Fresh install, no `history.sqlite` yet (or an empty one).** `/api/now` and `/api/usage` return 503 `{"error": "no history yet"}`, never a 500. The page shows "No data yet — is the monitor running?". Tested in Task 6.
2. **Monitor stopped.** The latest poll is older than `stale_after_s`, so `/api/now` returns `"stale": true` and the page shows a red "monitor not running since …" banner. Tested in Task 6.
3. **Gaps in the history** (service was down). `/api/usage` coverage is below 1.0 and nothing is interpolated. Tested in Task 5.
4. **Hostile or odd query params** (`from=2026-13-45`, `hours=-1`, `hours=abc`, `by=month`, a range over 366 days) get a 400 with no traceback. Tested in Task 6.
5. **Unattributed process (user None) and HTML-looking text.** The API emits `null`, and the page renders the literal text "unattributed" and only ever uses `textContent`, never `innerHTML`, for data. Tested in Task 5 (API) and Task 7 (static check on app.js).

---

## File Structure

```
resourcemonitor/
  energy.py      MODIFY: accumulate() returns (dt_s, list[EnergyRow]); EnergyRow dataclass
  history.py     CREATE: HistoryWriter, schema, prune. The only module that writes the DB.
  queries.py     CREATE: read-only query functions over a sqlite3.Connection (pure, no HTTP)
  web.py         CREATE: HTTP handler and server factory; routes → queries; validation; headers
  web/index.html CREATE: page skeleton (no inline JS/CSS)
  web/app.js     CREATE: fetch API, render cards/tables/SVG charts, auto-refresh
  web/app.css    CREATE: layout, light and dark via prefers-color-scheme
  cli.py         MODIFY: --history/--retention-days for watch; new `serve` mode
deploy/resourcemonitor-web.service   CREATE
tests/history_fixture.py             CREATE: build a synthetic multi-day history DB (tests + local preview)
tests/test_energy.py  test_history.py  test_queries.py  test_web.py  test_web_static.py  test_cli.py
```

---

### Task 1: Energy breakdown rows

**Files:**
- Modify: `resourcemonitor/energy.py`
- Modify: `resourcemonitor/cli.py` (only the `ledger.accumulate(snap)` call site, which ignores the return value for now)
- Test: `tests/test_energy.py`

**Interfaces:**
- Produces: `EnergyRow(gpu: int, bucket: str, kwh: float, seconds: float)`, a frozen dataclass in `energy.py`. It also produces the constants `IDLE_BUCKET = "(idle)"` and `UNATTRIBUTED_BUCKET = "(unattributed)"`. `EnergyLedger.accumulate(snap) -> tuple[float | None, list[EnergyRow]]`:
  - `(None, [])` when there is no interval (the first snapshot, or a gap / non-positive dt).
  - Otherwise `(dt_s, rows)`, with **one row per (gpu, bucket)** for the interval. Several processes of the same user on one GPU are merged into one row (kwh summed, seconds = dt_s once). An idle GPU gives one `IDLE_BUCKET` row with seconds = dt_s. Unattributed holders give an `UNATTRIBUTED_BUCKET` row.
- The ledger's existing totals must be unchanged (same arithmetic). The returned rows are a by-product.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_energy.py`; reuse that file's existing `_snap`/`T0` helpers)

```python
from resourcemonitor.energy import EnergyRow, IDLE_BUCKET, UNATTRIBUTED_BUCKET


def test_accumulate_returns_none_and_no_rows_without_an_interval():
    led = EnergyLedger()
    assert led.accumulate(_snap(T0, [GpuState(0, 81559, 0, 0, 70.0)])) == (None, [])


def test_accumulate_returns_one_row_per_gpu_and_bucket():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0), GpuState(0, 81559, 0, 0, 66.0)]
    p = [GpuProcess(1, 6, 10000, "a"), GpuProcess(2, 6, 10000, "a"),
         GpuProcess(3, 6, 10000, None)]
    led.accumulate(_snap(T0, g, p))

    dt, rows = led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert dt == 3600.0
    by = {(r.gpu, r.bucket): r for r in rows}
    assert set(by) == {(6, "a"), (6, UNATTRIBUTED_BUCKET), (0, IDLE_BUCKET)}
    assert round(by[(6, "a")].kwh, 3) == 0.400            # two procs merged: 2/3 of 0.6
    assert round(by[(6, UNATTRIBUTED_BUCKET)].kwh, 3) == 0.200
    assert round(by[(0, IDLE_BUCKET)].kwh, 3) == 0.066
    assert all(r.seconds == 3600.0 for r in rows)


def test_rows_sum_to_the_same_energy_the_ledger_books():
    led = EnergyLedger(max_gap_s=7200)
    g = [GpuState(6, 81559, 30000, 100, 600.0)]
    p = [GpuProcess(1, 6, 20000, "a"), GpuProcess(2, 6, 10000, "b")]
    led.accumulate(_snap(T0, g, p))
    _, rows = led.accumulate(_snap(T0 + timedelta(hours=1), g, p))

    assert round(sum(r.kwh for r in rows), 6) == round(led.totals()["per_gpu"][6], 6)


def test_a_gap_returns_no_rows():
    led = EnergyLedger(max_gap_s=300)
    g = [GpuState(0, 81559, 0, 100, 700.0)]
    led.accumulate(_snap(T0, g))
    assert led.accumulate(_snap(T0 + timedelta(hours=6), g)) == (None, [])
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m pytest tests/test_energy.py -v`
Expected: FAIL, `ImportError: cannot import name 'EnergyRow'`

- [ ] **Step 3: Implement.** In `energy.py`, add the dataclass and constants. Then change `accumulate` so it builds a `dict[(gpu, bucket)] -> [kwh]` alongside the existing totals and returns the rows:

```python
IDLE_BUCKET = "(idle)"
UNATTRIBUTED_BUCKET = "(unattributed)"


@dataclass(frozen=True)
class EnergyRow:
    gpu: int
    bucket: str           # username, IDLE_BUCKET or UNATTRIBUTED_BUCKET
    kwh: float
    seconds: float        # interval length this GPU was held by this bucket
```

The early returns become `return None, []`. Inside the GPU loop, the idle branch adds `kwh` to `acc[(g.index, IDLE_BUCKET)]`. A holder with `p.user is None` adds `kwh * share` to `acc[(g.index, UNATTRIBUTED_BUCKET)]`, and every other holder to `acc[(g.index, p.user)]`. Keep all existing ledger updates exactly as they are. Finish with:

```python
        return dt_s, [EnergyRow(gpu, bucket, kwh, dt_s) for (gpu, bucket), kwh in acc.items()]
```

In `cli.py` the call stays `ledger.accumulate(snap)` (the result is ignored in this task). Type-annotate `accumulate(self, snap: Snapshot)` and import `Snapshot` from `resourcemonitor.model`, dropping the `type: ignore`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass (previously 73 passed + 1 skipped, now 4 more).

- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/energy.py resourcemonitor/cli.py tests/test_energy.py
git commit -m "feat(energy): accumulate() returns the per-interval breakdown rows"
```

---

### Task 2: Process labels — the "what" in who-does-what-when

**Files:**
- Modify: `resourcemonitor/model.py`, `resourcemonitor/probe.py`
- Test: `tests/test_probe.py`, `tests/test_owner.py`

**Interfaces:**
- Produces:
  - `GpuProcess` gains a last field `name: str | None = None`, a short human label. It keeps its default, so every existing 3- and 4-argument construction still works.
  - `APP_FIELDS = "gpu_uuid,pid,used_memory,process_name"`.
  - `parse_apps_query` accepts lines with **4 fields** (it sets `name` from the raw `process_name`) and still accepts the legacy **3-field** lines (`name=None`). Split with `line.split(",", 3)`, because a process path may contain commas. `[N/A]` or empty process names give `None`.
  - `label_for(name: str | None, cmdline: list[str] | None) -> str | None` is a **pure** function that builds the short label:
    - If `cmdline` is non-empty and the basename of `cmdline[0]` matches `python`, `python3` or `python3.N`: return `"python -m <module>"` when `-m <module>` is present. Otherwise return `"python <basename of the first argument ending in .py>"`. Otherwise return `"python"`.
    - Otherwise, if `name` is set, return the basename of `name` (`/opt/kev/.venv/bin/python` gives `python`; `VLLM::Worker_TP0` stays as is).
    - Otherwise return the basename of `cmdline[0]`, or `None`.
    - Truncate the result to 60 characters.
    - **Never include any other argument.** The page is unauthenticated on the LAN, and arguments can contain tokens, paths or data names.
  - `cmdline_of(pid: int) -> list[str] | None` reads `/proc/<pid>/cmdline` and splits on NUL. It returns `None` on `FileNotFoundError`, `PermissionError`, `ProcessLookupError` or `OSError`.
  - `probe()` fills `name=label_for(p.name, cmdline_of(p.pid))` for each process.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_probe.py`)

```python
from resourcemonitor.probe import label_for, parse_apps_query


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
```

Append to `tests/test_owner.py`:

```python
def test_cmdline_of_a_dead_pid_is_none():
    from resourcemonitor.probe import cmdline_of
    assert cmdline_of(2 ** 22) is None
```

- [ ] **Step 2: Run them and watch them fail.** Run `python3 -m pytest tests/test_probe.py tests/test_owner.py -v`. Expected: `ImportError: cannot import name 'label_for'`.
- [ ] **Step 3: Implement** per the Interfaces block. The existing real-fixture test (`real_apps_query.txt`, 3 columns) must keep passing unchanged.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Verify on carrot** (read-only; SSH authorized). Sync with `rsync -a --exclude .git --exclude .superpowers --exclude __pycache__ --exclude .idea --exclude .pytest_cache --exclude .claude/settings.local.json ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/`. Then run:

```bash
ssh -o BatchMode=yes cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && ~/miniconda3/envs/resourcemonitor/bin/python -c "
from resourcemonitor.probe import probe
for p in sorted(probe().procs, key=lambda p: p.gpu_index): print(p.gpu_index, p.pid, p.user, p.name, p.used_mib)
"'
```

  Expected: each line shows a user and a short label such as `python kev_run.py` or `VLLM::Worker_TP0`, never an argument. Paste the output in the report. Do **not** restart any unit.
- [ ] **Step 6: Commit**

```bash
git add resourcemonitor/model.py resourcemonitor/probe.py tests/test_probe.py tests/test_owner.py
git commit -m "feat(probe): short process labels (what is running), never full command lines"
```

---

### Task 3: History writer

**Files:**
- Create: `resourcemonitor/history.py`
- Create: `tests/history_fixture.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: `Snapshot`, `GpuState`, `GpuProcess(pid, gpu_index, used_mib, user=None, name=None)` (model.py, `name` from Task 2); `EnergyRow` (Task 1); `Alert(kind, key, text, gpu_index, user)` (rules.py).
- Produces:
  - `SCHEMA_VERSION = 1`
  - `class HistoryWriter:`
    - `__init__(self, path: Path | str, retention_days: int = 90)` creates parent dirs, connects, sets `PRAGMA journal_mode=WAL`, and creates the schema if missing (`PRAGMA user_version = 1`)
    - `record(self, snap: Snapshot, dt_s: float | None, energy: list[EnergyRow], alerts: list[Alert], sent_keys: set[str]) -> None` writes one transaction
    - `prune(self, now: datetime) -> int` deletes rows with `ts < now - retention_days` from all five tables and returns the number of polls deleted. `record` calls it automatically at most once per UTC day.
    - `close(self) -> None`
  - Schema (exact):

```sql
CREATE TABLE IF NOT EXISTS polls (ts TEXT PRIMARY KEY, dt_s REAL);
CREATE TABLE IF NOT EXISTS gpu_samples (ts TEXT NOT NULL, gpu INTEGER NOT NULL,
  total_mib INTEGER NOT NULL, used_mib INTEGER NOT NULL, util_pct INTEGER NOT NULL,
  power_w REAL NOT NULL);
CREATE TABLE IF NOT EXISTS proc_samples (ts TEXT NOT NULL, gpu INTEGER NOT NULL,
  pid INTEGER NOT NULL, user TEXT, used_mib INTEGER NOT NULL, name TEXT);
CREATE TABLE IF NOT EXISTS energy_samples (ts TEXT NOT NULL, gpu INTEGER NOT NULL,
  bucket TEXT NOT NULL, kwh REAL NOT NULL, seconds REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (ts TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
  gpu INTEGER, user TEXT, text TEXT NOT NULL, sent INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS gpu_samples_ts ON gpu_samples(ts);
CREATE INDEX IF NOT EXISTS proc_samples_ts ON proc_samples(ts);
CREATE INDEX IF NOT EXISTS energy_samples_ts ON energy_samples(ts);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
```

  - `tests/history_fixture.py` provides `build_history(path, start: datetime, hours: int, interval_s: int = 300, gap: tuple[datetime, datetime] | None = None) -> None`. It drives a real `EnergyLedger(max_gap_s=interval_s*5)` and a real `HistoryWriter` over synthetic snapshots:
    - 8 GPUs; GPUs 6 and 7 held by `"dorian.zwanzig"` at ~575 W / 100 % (name `"python kev_run.py"`; GPU 6 restarts with a **new pid** every 6 hours so the timeline has several jobs).
    - GPU 5 held by `"cwinkelmann"` (name `"python train.py"`) at 300 W / 60 % during even hours, idle (66 W) otherwise.
    - GPU 4 holds an unattributed process (user None, name None, 2048 MiB) at 70 W / 0 %.
    - GPUs 0–3 idle at 66 W.
    - Every poll records one allocation Alert for dorian on GPU 6 (`sent` only on the first poll).
    - Polls inside `gap` are skipped (so the ledger returns `dt_s=None` after it).
    - Later tasks use this fixture; it must be deterministic (no `now()`).

- [ ] **Step 1: Write the failing tests** (`tests/test_history.py`)

```python
import sqlite3
from datetime import datetime, timedelta, timezone

from resourcemonitor.energy import EnergyRow
from resourcemonitor.history import HistoryWriter
from resourcemonitor.model import GpuProcess, GpuState, Snapshot
from resourcemonitor.rules import Alert

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _snap(t):
    return Snapshot(t, (GpuState(6, 81559, 22715, 100, 575.0),),
                    (GpuProcess(42, 6, 22706, "dorian.zwanzig", "python kev_run.py"),
                     GpuProcess(43, 6, 9, None, None)))


def _rows(db, sql):
    return sqlite3.connect(db).execute(sql).fetchall()


def test_record_writes_one_transaction_across_all_tables(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db)
    a = Alert(kind="allocation", key="allocation:dorian.zwanzig:6", text="x", gpu_index=6,
              user="dorian.zwanzig")
    w.record(_snap(T0), 60.0, [EnergyRow(6, "dorian.zwanzig", 0.0096, 60.0)], [a], {a.key})
    w.close()

    assert _rows(db, "SELECT ts, dt_s FROM polls") == [(T0.isoformat(), 60.0)]
    assert _rows(db, "SELECT gpu, used_mib, util_pct, power_w FROM gpu_samples") == [(6, 22715, 100, 575.0)]
    assert sorted(_rows(db, "SELECT pid, user, name FROM proc_samples"), key=lambda r: r[0]) == \
        [(42, "dorian.zwanzig", "python kev_run.py"), (43, None, None)]
    assert _rows(db, "SELECT bucket, kwh, seconds FROM energy_samples") == [("dorian.zwanzig", 0.0096, 60.0)]
    assert _rows(db, "SELECT kind, gpu, user, sent FROM alerts") == [("allocation", 6, "dorian.zwanzig", 1)]


def test_first_poll_records_null_interval(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db)
    w.record(_snap(T0), None, [], [], set())
    w.close()
    assert _rows(db, "SELECT dt_s FROM polls") == [(None,)]


def test_db_is_in_wal_mode_so_a_reader_never_blocks_the_writer(tmp_path):
    db = tmp_path / "h.sqlite"
    HistoryWriter(db).close()
    assert _rows(db, "PRAGMA journal_mode") == [("wal",)]


def test_reopening_an_existing_db_keeps_its_rows(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db); w.record(_snap(T0), None, [], [], set()); w.close()
    w = HistoryWriter(db); w.record(_snap(T0 + timedelta(minutes=1)), 60.0, [], [], set()); w.close()
    assert len(_rows(db, "SELECT ts FROM polls")) == 2


def test_prune_drops_rows_older_than_retention(tmp_path):
    db = tmp_path / "h.sqlite"
    w = HistoryWriter(db, retention_days=1)
    w.record(_snap(T0), None, [EnergyRow(6, "(idle)", 0.1, 60.0)], [], set())
    w.record(_snap(T0 + timedelta(days=2)), None, [], [], set())   # triggers the daily prune
    w.close()
    assert [r[0] for r in _rows(db, "SELECT ts FROM polls")] == [(T0 + timedelta(days=2)).isoformat()]
    assert _rows(db, "SELECT COUNT(*) FROM energy_samples") == [(0,)]


def test_fixture_builds_a_deterministic_history(tmp_path):
    from tests.history_fixture import build_history
    db = tmp_path / "h.sqlite"
    build_history(db, T0, hours=24)
    (n,) = _rows(db, "SELECT COUNT(*) FROM polls")[0]
    assert n == 24 * 12                                   # 5-minute polls
    buckets = {b for (b,) in _rows(db, "SELECT DISTINCT bucket FROM energy_samples")}
    assert buckets == {"dorian.zwanzig", "cwinkelmann", "(idle)", "(unattributed)"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m pytest tests/test_history.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'resourcemonitor.history'`

- [ ] **Step 3: Implement** `history.py`:
  - `executescript` the schema, then `PRAGMA user_version = 1`.
  - `record` uses `with self._conn:` (one transaction) and `executemany` for each table.
  - Each alert's `sent` is `1 if a.key in sent_keys else 0`.
  - The prune runs when `snap.taken_at.date()` differs from the last prune date (start with the last prune date as None, so the first `record` prunes once). The cutoff is `(now - timedelta(days=retention_days)).isoformat()`, compared as a string (ISO UTC strings sort chronologically).
  - Module docstring: "The only module that writes history. The web process reads it with mode=ro."

  Then implement `tests/history_fixture.py` as specified in Interfaces.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/history.py tests/history_fixture.py tests/test_history.py
git commit -m "feat(history): SQLite history writer with daily retention pruning"
```

---

### Task 4: Wire history into `watch`

**Files:**
- Modify: `resourcemonitor/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `HistoryWriter` (Task 3); `EnergyLedger.accumulate -> (dt_s, rows)` (Task 1).
- Produces:
  - `run_once(pol, state, notifier, tracker, ledger, host, energy_path, history=None) -> int`. When `history` is not None, after `state.save()` it calls `history.record(snap, dt_s, rows, alerts, {a.key for a in fresh})` inside `try/except (sqlite3.Error, OSError) as e: print(f"history write failed: {e.__class__.__name__}", flush=True)`.
  - New CLI flags: `--history PATH` (default `~/.local/state/resourcemonitor/history.sqlite`), `--no-history`, `--retention-days INT` (default 90). Only `watch` creates a `HistoryWriter`; `once` and `report` never write history.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_cli.py`, reusing the fake-probe / recording-notifier pattern of the existing `test_run_once_gates_repeat_alerts_and_saves_energy`. Read that test first and copy its setup.

```python
def test_run_once_records_history_with_sent_flags(tmp_path, monkeypatch):
    # same fake probe/notifier/policy/state/ledger setup as the existing gating test
    ...
    from resourcemonitor.history import HistoryWriter
    hist = HistoryWriter(tmp_path / "h.sqlite")
    run_once(pol, state, notifier, tracker, ledger, "carrot", tmp_path / "e.json", history=hist)
    run_once(pol, state, notifier, tracker, ledger, "carrot", tmp_path / "e.json", history=hist)
    hist.close()
    rows = sqlite3.connect(tmp_path / "h.sqlite").execute(
        "SELECT ts, sent FROM alerts ORDER BY ts").fetchall()
    assert [s for _, s in rows][:1] == [1] and rows[-1][1] == 0   # sent once, then suppressed


def test_a_broken_history_never_stops_alerts(tmp_path, monkeypatch, capsys):
    class Exploding:
        def record(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error at /secret/path")
    # same setup; the notifier must still receive the alert
    sent = run_once(pol, state, notifier, tracker, ledger, "carrot", tmp_path / "e.json",
                    history=Exploding())
    assert sent >= 1 and notifier.calls                     # alerts went out
    out = capsys.readouterr().out
    assert "history write failed: OperationalError" in out and "/secret/path" not in out


def test_history_flags_parse():
    a = build_parser().parse_args(["watch", "--retention-days", "30", "--no-history"])
    assert a.retention_days == 30 and a.no_history is True
```

The `...` stands for the setup copied from the existing gating test in the same file. The implementer writes it out in full, not by reference.

- [ ] **Step 2: Run them and watch them fail.** Run `python3 -m pytest tests/test_cli.py -v`. Expected: FAIL (unexpected keyword `history`; unknown flag).
- [ ] **Step 3: Implement.** In `run_once`, capture `dt_s, rows = ledger.accumulate(snap)`. In `main`, for `watch` only: `history = None if args.no_history else HistoryWriter(args.history, args.retention_days)`, then pass it to every `run_once` call. If the `HistoryWriter` constructor itself raises `sqlite3.Error`/`OSError`, print `history disabled: <ClassName>` and continue with `history=None`; monitoring must start regardless.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/cli.py tests/test_cli.py
git commit -m "feat(cli): watch records every poll to history; failures never stop alerts"
```

---

### Task 5: Read-only queries

**Files:**
- Create: `resourcemonitor/queries.py`
- Test: `tests/test_queries.py`

**Interfaces:**
- Consumes: the schema (Task 3); `build_history` (Task 3); `IDLE_BUCKET`, `UNATTRIBUTED_BUCKET` (Task 1).
- Produces (all pure functions over an open connection; no HTTP, no clock except the explicit `now` argument):
  - `class NoHistory(Exception)`, raised when the file is missing or has no polls.
  - `open_ro(path) -> sqlite3.Connection`: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. Raises `NoHistory` if the file does not exist.
  - `latest(conn, assignments: dict[str, frozenset[int]], now: datetime, stale_after_s: int) -> dict` returns:
    `{"ts": str, "age_s": float, "stale": bool, "gpus": [{"gpu": int, "total_mib": int, "used_mib": int, "util_pct": int, "power_w": float, "assigned_to": str|None, "procs": [{"pid": int, "user": str|None, "name": str|None, "used_mib": int}]}], "alerts": [{"kind","key","gpu","user","text","sent"}]}`. GPUs are sorted by index. `stale` is `age_s > stale_after_s`. Raises `NoHistory` if `polls` is empty.
  - `usage(conn, start: date, end: date, by: str, now: datetime) -> dict` (`by` is `"day"` or `"week"`; `end` inclusive; days are UTC) returns:
    `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "by": by, "periods": [{"start": "YYYY-MM-DD", "kwh": {bucket: float}, "gpu_hours": {bucket: float}}], "totals": {"kwh": {bucket: float}, "gpu_hours": {bucket: float}}, "coverage": {"monitored_s": float, "elapsed_s": float, "ratio": float}, "caveats": [str, str]}`.
    - kWh is `SUM(kwh)`; GPU-hours is `SUM(seconds)/3600` from `energy_samples`, grouped by `substr(ts,1,10)` and bucket. Week periods merge days by ISO-week Monday.
    - `monitored_s` is `SUM(dt_s)` over polls in range. `elapsed_s` is the seconds from `max(start 00:00Z, first poll ts)` to `min(end+1day 00:00Z, now)`, minimum 0. `ratio` is `monitored_s/elapsed_s`, or 0.0 when `elapsed_s` is 0.
    - Periods with no data are present, with empty dicts, so the chart's x-axis is continuous.
    - `caveats` are exactly the two strings in Global Constraints.
  - `timeseries(conn, hours: int, now: datetime, max_points: int = 300) -> dict` returns `{"gpus": {"0": [{"ts": str, "power_w": float, "util_pct": float}], ...}}`. It covers rows with `ts >= now - hours`. When a GPU has more than `max_points` rows, consecutive rows are averaged in equal-size chunks (`ceil(n/max_points)`) and each point carries the chunk's first `ts`.
  - `timeline(conn, hours: int, now: datetime) -> dict` — **who does what when.** Returns `{"from": iso, "to": iso, "gpus": {"0": [job, ...], ..., "7": [...]}}`. Every GPU index seen in `gpu_samples` in the window is present (empty list if idle). A `job` is `{"pid": int, "user": str|None, "name": str|None, "start": iso, "end": iso, "max_mib": int, "ongoing": bool}`: the maximal run of **consecutive polls** (ordered by `polls.ts`) in which that `(gpu, pid)` appears. A run ends when a poll lacks the pid, **or** when the next poll has `dt_s IS NULL` (a monitoring gap, so a job that spans a gap is split, never bridged). `start`/`end` are the first/last poll timestamps of the run. `ongoing` is true when the run includes the latest poll overall. Only runs with at least one sample in `[now - hours, now]` are returned, sorted by `start`.

- [ ] **Step 1: Write the failing tests** (`tests/test_queries.py`)

```python
from datetime import date, datetime, timedelta, timezone

import pytest

from resourcemonitor.queries import NoHistory, latest, open_ro, timeline, timeseries, usage
from tests.history_fixture import build_history

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
ASSIGN = {"dorian.zwanzig": frozenset({0, 1, 2, 3}), "cwinkelmann": frozenset({4, 5, 6, 7})}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "h.sqlite"
    build_history(p, T0, hours=48,
                  gap=(T0 + timedelta(hours=30), T0 + timedelta(hours=36)))
    return p


def test_missing_file_is_no_history_not_a_crash(tmp_path):
    with pytest.raises(NoHistory):
        open_ro(tmp_path / "absent.sqlite")


def test_reader_cannot_write(db):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        open_ro(db).execute("DELETE FROM polls")


def test_latest_reports_gpus_owners_and_assignees(db):
    now = T0 + timedelta(hours=48)
    d = latest(open_ro(db), ASSIGN, now, stale_after_s=600)
    assert d["stale"] is False and len(d["gpus"]) == 8
    g6 = d["gpus"][6]
    assert g6["assigned_to"] == "cwinkelmann"
    assert {p["user"] for p in g6["procs"]} == {"dorian.zwanzig"}
    assert d["gpus"][4]["procs"][0]["user"] is None          # unattributed stays null
    assert any(a["kind"] == "allocation" for a in d["alerts"])


def test_latest_is_stale_when_the_monitor_stopped(db):
    d = latest(open_ro(db), ASSIGN, T0 + timedelta(days=3), stale_after_s=600)
    assert d["stale"] is True and d["age_s"] > 600


def test_usage_by_day_has_continuous_periods_and_caveats(db):
    u = usage(open_ro(db), date(2026, 10, 4), date(2026, 10, 7), "day",
              now=T0 + timedelta(hours=48))
    assert [p["start"] for p in u["periods"]] == ["2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"]
    assert u["periods"][0]["kwh"] == {}                      # before monitoring began
    assert u["totals"]["kwh"]["dorian.zwanzig"] > u["totals"]["kwh"]["cwinkelmann"] > 0
    assert "(idle)" in u["totals"]["kwh"] and "(unattributed)" in u["totals"]["kwh"]
    assert len(u["caveats"]) == 2


def test_usage_coverage_reflects_the_gap(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 6), "day",
              now=T0 + timedelta(hours=48))
    assert 0.80 < u["coverage"]["ratio"] < 0.92               # 6 h of 48 missing, nothing invented


def test_usage_by_week_merges_days(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 6), "week",
              now=T0 + timedelta(hours=48))
    assert [p["start"] for p in u["periods"]] == ["2026-10-05"]   # Monday


def test_gpu_hours_for_two_full_gpus_over_a_day(db):
    u = usage(open_ro(db), date(2026, 10, 5), date(2026, 10, 5), "day",
              now=T0 + timedelta(hours=48))
    assert 47.0 < u["totals"]["gpu_hours"]["dorian.zwanzig"] <= 48.0   # GPUs 6+7 × 24 h


def test_timeline_shows_who_did_what_when(tmp_path):
    p = tmp_path / "h.sqlite"
    build_history(p, T0, hours=48)
    tl = timeline(open_ro(p), hours=48, now=T0 + timedelta(hours=48))
    g6 = tl["gpus"]["6"]
    assert len(g6) == 8                                      # new pid every 6 h
    assert {(j["user"], j["name"]) for j in g6} == {("dorian.zwanzig", "python kev_run.py")}
    assert [j["ongoing"] for j in g6] == [False] * 7 + [True]
    assert all(j["start"] < j["end"] for j in g6)
    assert tl["gpus"]["4"][0]["user"] is None                # unattributed stays null
    assert tl["gpus"]["0"] == []                              # idle GPU present, no jobs


def test_timeline_splits_a_job_at_a_monitoring_gap(db):
    tl = timeline(open_ro(db), hours=48, now=T0 + timedelta(hours=48))
    assert len(tl["gpus"]["7"]) == 2                          # one pid, but the 6 h gap splits it


def test_timeline_window_excludes_old_jobs(db):
    tl = timeline(open_ro(db), hours=1, now=T0 + timedelta(hours=48))
    assert len(tl["gpus"]["6"]) == 1


def test_timeseries_is_downsampled(db):
    ts = timeseries(open_ro(db), hours=48, now=T0 + timedelta(hours=48), max_points=50)
    assert set(ts["gpus"]) == {str(i) for i in range(8)}
    assert all(len(v) <= 50 for v in ts["gpus"].values())
```

- [ ] **Step 2: Run them and watch them fail.** `python3 -m pytest tests/test_queries.py -v` gives `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `queries.py` per the Interfaces block.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/queries.py tests/test_queries.py
git commit -m "feat(queries): read-only history queries for now, timeline, usage and timeseries"
```

---

### Task 6: HTTP server and `serve` mode

**Files:**
- Create: `resourcemonitor/web.py`
- Create: `resourcemonitor/web/index.html`, `resourcemonitor/web/app.js`, `resourcemonitor/web/app.css` (minimal placeholders in this task: index.html links app.css and app.js and contains `<main id="app"></main>`; Task 7 fills them)
- Modify: `resourcemonitor/cli.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `open_ro`, `latest`, `usage`, `timeseries`, `timeline`, `NoHistory` (Task 5); `load_policy` (policy.py).
- Produces:
  - `make_server(bind: str, port: int, history_path: Path, policy_path: Path, stale_after_s: int = 180, clock=lambda: datetime.now(timezone.utc)) -> ThreadingHTTPServer`. `port=0` is allowed in tests.
  - CLI: mode `serve` with `--bind` (default `127.0.0.1`), `--port` (default 8765), `--stale-after` (default 180), and the existing `--history` and `--policy`. `serve` must not require `SLACK_WEBHOOK_URL`, must not construct a `Notifier`/`State`/`EnergyLedger`, and must not import probe or notify at module level in `web.py`. In `cli.main`, dispatch `serve` **before** any notifier/state setup.
  - Routes and status codes exactly as in Global Constraints:
    - `/api/usage` takes `from`, `to` (YYYY-MM-DD; defaults: to = today UTC, from = to − 13 days) and `by` (`day`|`week`, default `day`).
    - `/api/timeseries` takes `hours` (default 24, 1..168).
    - `/api/timeline` takes `hours` (default 24, **1..720**, i.e. up to 30 days).
    - `/healthz` returns `{"ok": true, "age_s": float}`, or 503 `{"ok": false}` when there is no history.
    - Policy is read per request. If the policy file is unreadable, `assigned_to` is null for every GPU (no 500).
  - Static files are read from `Path(__file__).parent / "web" / <fixed name>` with content types `text/html; charset=utf-8`, `text/javascript; charset=utf-8` and `text/css; charset=utf-8`.

- [ ] **Step 1: Write the failing tests** (`tests/test_web.py`). They start a real server on `127.0.0.1:0` in a thread and use `urllib.request` against it.

```python
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from resourcemonitor.web import make_server
from tests.history_fixture import build_history

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=48)
POLICY = """[assignments]
"dorian.zwanzig" = [0, 1, 2, 3]
"cwinkelmann" = [4, 5, 6, 7]
[rules]
idle_util_pct = 5
idle_min_mib = 1024
idle_grace_s = 1800
capacity_free_mib = 40960
[notify]
cooldown_s = 3600
channel = "#gpu-watch"
"""


def _serve(tmp_path, with_history=True, now=NOW):
    hist = tmp_path / "h.sqlite"
    if with_history:
        build_history(hist, T0, hours=48)
    pol = tmp_path / "policy.toml"; pol.write_text(POLICY)
    srv = make_server("127.0.0.1", 0, hist, pol, stale_after_s=600, clock=lambda: now)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def live(tmp_path):
    srv, base = _serve(tmp_path)
    yield base
    srv.shutdown()


def test_index_and_assets_are_served_with_security_headers(live):
    for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")]:
        status, headers, _ = _get(live + path)
        assert status == 200 and headers["Content-Type"].startswith(ctype)
        assert headers["Content-Security-Policy"] == "default-src 'self'"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers


def test_api_now_returns_live_view(live):
    status, _, body = _get(live + "/api/now")
    d = json.loads(body)
    assert status == 200 and d["stale"] is False and d["gpus"][6]["assigned_to"] == "cwinkelmann"


def test_api_usage_defaults_and_params(live):
    status, _, body = _get(live + "/api/usage?from=2026-10-05&to=2026-10-06&by=day")
    d = json.loads(body)
    assert status == 200 and len(d["periods"]) == 2 and len(d["caveats"]) == 2


@pytest.mark.parametrize("q", [
    "/api/usage?from=2026-13-45", "/api/usage?by=month", "/api/usage?from=2020-01-01&to=2026-10-06",
    "/api/usage?from=2026-10-06&to=2026-10-01", "/api/timeseries?hours=-1",
    "/api/timeseries?hours=abc", "/api/timeseries?hours=169",
    "/api/timeline?hours=0", "/api/timeline?hours=721"])
def test_bad_params_are_400_without_a_traceback(live, q):
    status, _, body = _get(live + q)
    assert status == 400 and json.loads(body) == {"error": "bad request"}
    assert b"Traceback" not in body


def test_api_timeline_returns_jobs_per_gpu(live):
    status, _, body = _get(live + "/api/timeline?hours=48")
    d = json.loads(body)
    assert status == 200 and d["gpus"]["6"] and d["gpus"]["6"][0]["name"] == "python kev_run.py"


def test_unknown_path_is_404_and_post_is_405(live):
    assert _get(live + "/../../etc/passwd")[0] == 404
    assert _get(live + "/api/now", method="POST")[0] == 405


def test_fresh_install_without_history_is_503_not_500(tmp_path):
    srv, base = _serve(tmp_path, with_history=False)
    try:
        status, _, body = _get(base + "/api/now")
        assert status == 503 and json.loads(body) == {"error": "no history yet"}
        assert _get(base + "/healthz")[0] == 503
        assert _get(base + "/")[0] == 200                  # the page itself still loads
    finally:
        srv.shutdown()


def test_stopped_monitor_is_reported_stale(tmp_path):
    srv, base = _serve(tmp_path, now=NOW + timedelta(hours=2))
    try:
        assert json.loads(_get(base + "/api/now")[2])["stale"] is True
    finally:
        srv.shutdown()


def test_serve_does_not_need_a_webhook(monkeypatch):
    from resourcemonitor.cli import build_parser
    a = build_parser().parse_args(["serve", "--bind", "10.188.1.1", "--port", "8765"])
    assert (a.mode, a.bind, a.port) == ("serve", "10.188.1.1", 8765)


def test_web_module_never_imports_probe_or_notify():
    import ast, pathlib
    src = pathlib.Path("resourcemonitor/web.py").read_text()
    names = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("probe" in m or "notify" in m) for m in names)
```

- [ ] **Step 2: Run them and watch them fail.** `python3 -m pytest tests/test_web.py -v` gives `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `web.py`:
  - a `BaseHTTPRequestHandler` subclass with `do_GET` dispatching on `urllib.parse.urlsplit(self.path).path` through a dict of fixed routes;
  - `do_POST`/`do_PUT`/`do_DELETE`/`do_PATCH` send 405;
  - one `_send(status, body: bytes, ctype)` helper that always adds the two security headers;
  - `log_message` overridden to do nothing;
  - parameter parsing that raises `ValueError` on anything invalid, caught and mapped to 400 `{"error": "bad request"}`;
  - `NoHistory` mapped to 503 `{"error": "no history yet"}`;
  - any other exception mapped to 500 `{"error": "internal"}`, printing only the exception class.

  Then add the `serve` mode in `cli.py`.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add resourcemonitor/web.py resourcemonitor/web/ resourcemonitor/cli.py tests/test_web.py
git commit -m "feat(web): read-only HTTP server and JSON API (serve mode)"
```

---

### Task 7: The page

**Files:**
- Modify: `resourcemonitor/web/index.html`, `resourcemonitor/web/app.js`, `resourcemonitor/web/app.css`
- Test: `tests/test_web_static.py`

**Interfaces:**
- Consumes: the JSON shapes of `/api/now`, `/api/usage` and `/api/timeseries` (Task 6/6 Interfaces, exactly).
- Produces: a single page with no inline script/style and no external URLs.

**Design (decided in this plan):**
- **Header bar:** title "carrot GPUs", the time of the last poll (local time plus "UTC" in a tooltip), an auto-refresh indicator, and a red full-width banner when `stale` ("Monitor not running since <time> — numbers below are not live"). With 503 "no history yet", it shows "No data yet — is the monitor running?" in place of all sections.
- **Now** (refresh every 30 s): a responsive grid of 8 GPU cards (4 columns on desktop, 2 on tablet, 1 on phone). Each card shows:
  - `GPU n` and `assigned to <user|—>`;
  - a VRAM bar (used/total, coloured by the holding user's colour);
  - `util %` and `W`;
  - the process list as `user · what · MiB` (what = `name`, omitted when null), showing `unattributed` in italics when the user is null.
  - A card where any process user differs from `assigned_to` (and is not null) gets a red border and an "outside allocation" tag.
  - Below the grid, the current alerts list shows each alert's icon by kind plus its text, with "(Slack: sent)" or "(Slack: in cooldown)" from `sent`.
- **Timeline — who does what when** (directly under Now; refresh every 60 s): a Gantt chart in one SVG.
  - **Layout.** One row per GPU, labelled `GPU n · assigned to <user|—>`. The x-axis is time, with sensible ticks (hours for 24 h, days for 7 d/30 d) in local time and a "now" marker. Range buttons are `24 h | 7 d | 30 d` (default 24 h).
  - **Bars.** One bar per job from `start` to `end` (`ongoing` jobs extend to now with an open right edge), filled with the user's colour; unattributed is amber with a dashed outline. A bar whose user is non-null and differs from the GPU's `assigned_to` gets a red outline. The label inside the bar is `user · name` when it fits (≥ 120 px), otherwise none.
  - **Tooltip.** Every bar has an SVG `<title>` child with user (or "unattributed"), what (name or "unknown"), pid, local start–end, duration ("3 h 12 min") and peak VRAM. Set it with `textContent`.
  - **Legend.** A legend of user colours sits under the chart.
- **Usage** (fetch on load and when controls change; refresh every 5 min):
  - controls: range buttons `7 d | 30 d | 90 d` (default 7 d) and a toggle `by day | by week`;
  - a stacked-bar SVG chart of kWh per period, stacked by bucket;
  - a table with one row per bucket, columns `kWh`, `~€ at 0.30/kWh` and `GPU-hours`, sorted by kWh descending;
  - a coverage line ("Monitor was running for 87 % of this period"), shown in amber when below 95 %;
  - the two caveats verbatim, in small text under the table.
- **Last 24 h** (refresh every 5 min): eight small multiples, one per GPU, each an SVG sparkline of power (W, line) with utilisation as a light area behind it. Axes are minimal: a 0–700 W scale label and the start/end times.
- **Colours:** a fixed 8-colour palette assigned to users in alphabetical order. `(idle)` is neutral grey and `(unattributed)` is amber with a dashed outline. Both light and dark come from `prefers-color-scheme`, with all colours as CSS custom properties.
- **Safety:** all data goes into the DOM through `textContent` or `createElementNS` attributes. **Never `innerHTML` with data.** SVG is built with `document.createElementNS`.
- **Failures:** a failed fetch shows a small "couldn't reach server (retrying)" note and keeps the last rendered data.

- [ ] **Step 1: Write the failing static tests** (`tests/test_web_static.py`)

```python
import re
from pathlib import Path

WEB = Path("resourcemonitor/web")


def test_no_inline_script_or_style_because_csp_forbids_it():
    html = (WEB / "index.html").read_text()
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None
    assert "<style" not in html and " style=" not in html
    assert 'src="/app.js"' in html and 'href="/app.css"' in html


def test_no_external_urls_anywhere():
    for f in ("index.html", "app.js", "app.css"):
        assert re.search(r"https?://", (WEB / f).read_text()) is None, f


def test_data_never_goes_through_innerHTML():
    js = (WEB / "app.js").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js


def test_page_has_the_three_sections_and_caveat_slot():
    html = (WEB / "index.html").read_text()
    for id_ in ("now", "timeline", "usage", "timeseries", "caveats", "stale-banner"):
        assert f'id="{id_}"' in html
```

- [ ] **Step 2: Run them and watch them fail** (the placeholder from Task 6 lacks the section ids).
- [ ] **Step 3: Implement** the three files per the design above.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Local visual check.** Build a preview DB and run the server:

```bash
python3 -c "from datetime import datetime,timezone,timedelta; from tests.history_fixture import build_history; build_history('/tmp/rm-preview.sqlite', datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)-timedelta(hours=72), hours=72)"
python3 -m resourcemonitor serve --history /tmp/rm-preview.sqlite --policy deploy/policy.example.toml --port 8765 --stale-after 100000
```

  Open `http://127.0.0.1:8765` and confirm that all four sections render (Now, Timeline, Usage, Last 24 h), the dark/light themes work, and there are no console errors. Then stop the server.
- [ ] **Step 6: Commit**

```bash
git add resourcemonitor/web/ tests/test_web_static.py
git commit -m "feat(web): dashboard page — live GPU cards, timeline, usage chart/table, 24 h sparklines"
```

---

### Task 8: Deploy unit, docs, and rollout to carrot

**Files:**
- Create: `deploy/resourcemonitor-web.service`
- Modify: `deploy/resourcemonitor.service` (ExecStart only), `README.md`, `CLAUDE.md` (the project section only), `docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md` ("Reaching carrot" interpreter paragraph only), `.claude/skills/deploy-resourcemonitor/SKILL.md`, `.claude/skills/gpu-energy-report/SKILL.md`

**Interpreter change (user directive, 2026-10-06):** both services now run in the conda env `resourcemonitor` on carrot (`~/miniconda3/envs/resourcemonitor`, Python 3.12, created already). Both units use `ExecStart=%h/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor …`. Every doc that currently says the service must run on `/usr/bin/python3` ("do not fix it") must be updated to say: it runs in the `resourcemonitor` conda env by the user's choice; the code stays stdlib-only; and **if that env is removed or renamed, the monitor stops**, so recreate it with `~/miniconda3/bin/conda create -y -n resourcemonitor python=3.12 pytest`.
- Test: `tests/test_skills_present.py` (extend)

**Interfaces:**
- Consumes: `serve` mode (Task 6); history default path (Task 4).

- [ ] **Step 1: Write the unit**

```ini
# deploy/resourcemonitor-web.service  ->  ~/.config/systemd/user/
[Unit]
Description=GPU resource monitor dashboard (read-only, LAN, no auth)

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=%h/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor serve --bind 10.188.1.1 --port 8765
WorkingDirectory=%h/ResourceMonitor
Restart=always
RestartSec=10
# Same reasoning as resourcemonitor.service: no ProtectSystem/ProtectHome/PrivateTmp
# in a user unit (they imply PrivateUsers=yes). The process only reads history (mode=ro).
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Failing test.** Extend `tests/test_skills_present.py`:

```python
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
```

- [ ] **Step 3: Docs.**
  - README gets a "Dashboard" section: URL `http://10.188.1.1:8765`, what each section shows, that it is unauthenticated and LAN-visible by choice, that it is read-only, the history location and retention, and the two energy caveats.
  - `CLAUDE.md` gets one line under Layout: `history.py` writes and `web.py` reads with `mode=ro`, and `web.py` must never import probe/notify.
  - Deploy skill gets a new step "Dashboard": copy the web unit, `daemon-reload`, `enable --now resourcemonitor-web`, then verify with `curl -s http://10.188.1.1:8765/healthz` on carrot. It also notes the firewall caveat: if the LAN can't reach 8765, ask carrot's admin. Nothing else in the skill changes.
  - gpu-energy-report skill gets one line: the dashboard's Usage section shows the same numbers with history.
- [ ] **Step 4: Run** `python3 -m pytest -q`. Expected: all pass. Then commit:

```bash
git add deploy/ README.md CLAUDE.md docs/superpowers/plans/2026-10-06-gpu-resource-monitor.md .claude/skills tests/test_skills_present.py
git commit -m "feat(deploy): dashboard systemd unit and docs"
```

- [ ] **Step 5: Roll out to carrot** (SSH authorized; always `ssh -o BatchMode=yes cwinkelmann@10.188.1.1`). Never pass `--post` and never touch `~/.config/resourcemonitor/env`.
  1. `rsync -a --exclude .git --exclude .superpowers --exclude __pycache__ --exclude .idea --exclude .pytest_cache --exclude .claude/settings.local.json ./ cwinkelmann@10.188.1.1:~/ResourceMonitor/`
  2. Replace the dry soak so it runs in the conda env and writes history:
     - `systemctl --user stop resourcemonitor-soak`
     - `systemd-run --user --unit=resourcemonitor-soak --working-directory=/home/cwinkelmann/ResourceMonitor -p Restart=always -p RestartSec=30 -E PYTHONUNBUFFERED=1 /home/cwinkelmann/miniconda3/envs/resourcemonitor/bin/python -m resourcemonitor watch --interval 60` (no `--post`).
     Wait 3 minutes, then confirm that `~/.local/state/resourcemonitor/history.sqlite` exists, `polls` has ≥2 rows, and `proc_samples.name` is populated (`~/miniconda3/envs/resourcemonitor/bin/python -c` with sqlite3).
  3. Copy the web unit to `~/.config/systemd/user/`, then `systemctl --user daemon-reload && systemctl --user enable --now resourcemonitor-web`.
  4. On carrot, run `curl -s http://10.188.1.1:8765/healthz` and `curl -s http://10.188.1.1:8765/api/now | head -c 400`, and paste the outputs.
  5. Report `systemctl --user status resourcemonitor-web --no-pager | head -12`.
