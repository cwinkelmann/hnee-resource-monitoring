"""Pure read-only queries over the history database. No HTTP, no clock except `now`."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from math import ceil
from pathlib import Path

from resourcemonitor.claims import MIB_PER_GIB, Booking, allocate, taken_segments
from resourcemonitor.energy import UNATTRIBUTED_BUCKET

CAVEATS = [
    "Measured only while the monitor was running.",
    "Per-user kWh splits a card's draw by memory share when several processes share it "
    "— an estimate, not a measurement.",
]


class NoHistory(Exception):
    """The history file is missing or holds no polls yet."""


def open_ro(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise NoHistory(str(path))
    # as_uri() percent-encodes '?', '#' and '%', so they cannot leak into the query string.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _require_polls(conn: sqlite3.Connection) -> None:
    """A history file that exists but holds no poll yet is 'no history', not empty data."""
    if conn.execute("SELECT 1 FROM polls LIMIT 1").fetchone() is None:
        raise NoHistory("no polls")


def _booking_now(b: Booking, effective_mib: int, upcoming: list[Booking], total: int,
                 now: datetime) -> dict:
    d = b.to_json()
    d["effective_mib"] = effective_mib
    d["taken"] = [_seg_json(s) for s in taken_segments(b, upcoming, total, now, b.end)]
    return d


def _seg_json(s: dict) -> dict:
    return {"start": s["start"].isoformat(), "end": s["end"].isoformat(),
            "vram_mib": s["vram_mib"], "vram_gib": round(s["vram_mib"] / MIB_PER_GIB, 1),
            "by": s["by"]}


def latest(conn: sqlite3.Connection, bookings: list[Booking],
           now: datetime, stale_after_s: int, upcoming: list[Booking] | None = None) -> dict:
    """`bookings` are those active now; `upcoming` (uncancelled, from now on) lets each
    lendable one report the segments where important bookings take part of it."""
    upcoming = bookings if upcoming is None else upcoming
    ts = conn.execute("SELECT MAX(ts) FROM polls").fetchone()[0]
    if ts is None:
        raise NoHistory("no polls")
    age_s = (now - datetime.fromisoformat(ts)).total_seconds()
    booked: dict[int, list[Booking]] = {}
    for b in sorted(bookings, key=lambda b: (b.start, b.id)):
        booked.setdefault(b.gpu, []).append(b)
    procs: dict[int, list[dict]] = {}
    for gpu, pid, user, name, used in conn.execute(
            "SELECT gpu, pid, user, name, used_mib FROM proc_samples WHERE ts=? "
            "ORDER BY used_mib DESC, pid", (ts,)):
        procs.setdefault(gpu, []).append(
            {"pid": pid, "user": user, "name": name, "used_mib": used})
    gpus = []
    for g, total, used, util, power in conn.execute(
            "SELECT gpu, total_mib, used_mib, util_pct, power_w FROM gpu_samples "
            "WHERE ts=? ORDER BY gpu", (ts,)):
        active = booked.get(g, [])
        eff = allocate(active, total)
        gpus.append({"gpu": g, "total_mib": total, "used_mib": used, "util_pct": util,
                     "power_w": power,
                     "bookings": [_booking_now(b, eff[b.id], upcoming, total, now)
                                  for b in active],
                     "booked_mib": (bm := sum(eff.values())),
                     "free_mib": max(total - bm, 0), "procs": procs.get(g, [])})
    alerts = [{"kind": k, "key": key, "gpu": gpu, "user": user, "text": text, "sent": bool(sent)}
              for k, key, gpu, user, text, sent in conn.execute(
                  "SELECT kind, key, gpu, user, text, sent FROM alerts WHERE ts=? "
                  "ORDER BY rowid", (ts,))]
    return {"ts": ts, "age_s": age_s, "stale": age_s > stale_after_s,
            "slack": _slack_mode(conn), "gpus": gpus, "alerts": alerts,
            "box": _box_now(conn, ts)}


def _box_now(conn: sqlite3.Connection, ts: str) -> dict | None:
    """CPU and RAM at poll ts; None if that poll has none (or the history predates them)."""
    try:
        row = conn.execute(
            "SELECT ncpu, cores_busy, load1, mem_total_mib, mem_used_mib, swap_total_mib, "
            "swap_used_mib FROM host_samples WHERE ts = ?", (ts,)).fetchone()
    except sqlite3.OperationalError:            # written by a monitor without CPU/RAM
        return None
    if row is None:
        return None
    users = [{"user": u, "cores": c, "rss_mib": r} for u, c, r in conn.execute(
        "SELECT user, cores, rss_mib FROM user_samples WHERE ts = ? "
        "ORDER BY COALESCE(cores, 0) DESC, rss_mib DESC, user", (ts,))]
    keys = ("ncpu", "cores_busy", "load1", "mem_total_mib", "mem_used_mib", "swap_total_mib",
            "swap_used_mib")
    return {**dict(zip(keys, row)), "users": users}


def _slack_mode(conn: sqlite3.Connection) -> str | None:
    """'dry-run' | 'posting' as the monitor last recorded it; None for older DBs."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'slack'").fetchone()
    except sqlite3.OperationalError:          # no meta table: written before it existed
        return None
    return row[0] if row and row[0] in ("dry-run", "posting") else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def usage(conn: sqlite3.Connection, start: date, end: date, by: str, now: datetime) -> dict:
    if by not in ("day", "week"):
        raise ValueError(f"by must be 'day' or 'week', not {by!r}")
    _require_polls(conn)
    key = (lambda d: _monday(d)) if by == "week" else (lambda d: d)
    step = timedelta(days=7 if by == "week" else 1)
    periods: dict[date, dict] = {}
    p = key(start)
    while p <= end:
        periods[p] = {"start": p.isoformat(), "kwh": {}, "gpu_hours": {}}
        p += step
    totals = {"kwh": {}, "gpu_hours": {}}
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    for day, bucket, kwh, seconds in conn.execute(
            "SELECT substr(ts,1,10), bucket, SUM(kwh), SUM(seconds) FROM energy_samples "
            "WHERE ts >= ? AND ts < ? GROUP BY 1, 2", (lo, hi)):
        period = periods[key(date.fromisoformat(day))]
        for field, value in (("kwh", kwh), ("gpu_hours", seconds / 3600)):
            period[field][bucket] = period[field].get(bucket, 0.0) + value
            totals[field][bucket] = totals[field].get(bucket, 0.0) + value
    # A card shared by several buckets in one poll has a row per bucket, each with the full
    # interval: the per-bucket hours are right, but their sum would count that card twice.
    (card_s,) = conn.execute(
        "SELECT COALESCE(SUM(s), 0) FROM (SELECT MAX(seconds) AS s FROM energy_samples "
        "WHERE ts >= ? AND ts < ? GROUP BY ts, gpu)", (lo, hi)).fetchone()
    monitored_s, first = conn.execute(
        "SELECT COALESCE(SUM(dt_s), 0), MIN(ts) FROM polls WHERE ts >= ? AND ts < ?",
        (lo, hi)).fetchone()
    elapsed_s = 0.0
    since = None          # UTC date coverage is measured from: max(range start, first poll)
    first_any = conn.execute("SELECT MIN(ts) FROM polls").fetchone()[0]
    if first_any is not None:
        t0 = max(datetime.combine(start, datetime.min.time(), timezone.utc),
                 datetime.fromisoformat(first_any))
        t1 = min(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc), now)
        elapsed_s = max(0.0, (t1 - t0).total_seconds())
        if elapsed_s:
            since = t0.astimezone(timezone.utc).date().isoformat()
    ratio = monitored_s / elapsed_s if elapsed_s else 0.0
    return {"from": start.isoformat(), "to": end.isoformat(), "by": by,
            "periods": list(periods.values()), "totals": totals,
            "gpu_hours_total": card_s / 3600,
            "coverage": {"monitored_s": float(monitored_s), "elapsed_s": elapsed_s, "ratio": ratio,
                         "since": since},
            "caveats": list(CAVEATS)}


def timeseries(conn: sqlite3.Connection, hours: int, now: datetime, max_points: int = 300) -> dict:
    _require_polls(conn)
    since = (now - timedelta(hours=hours)).isoformat()
    rows: dict[int, list[tuple]] = {}
    for ts, gpu, power, util in conn.execute(
            "SELECT ts, gpu, power_w, util_pct FROM gpu_samples WHERE ts >= ? ORDER BY gpu, ts",
            (since,)):
        rows.setdefault(gpu, []).append((ts, power, util))
    out: dict[str, list[dict]] = {}
    for gpu, series in rows.items():
        size = max(1, ceil(len(series) / max_points))
        points = []
        for i in range(0, len(series), size):
            chunk = series[i:i + size]
            points.append({"ts": chunk[0][0],
                           "power_w": sum(c[1] for c in chunk) / len(chunk),
                           "util_pct": sum(c[2] for c in chunk) / len(chunk)})
        out[str(gpu)] = points
    return {"gpus": out}


def vram_timeseries(conn: sqlite3.Connection, hours: int, now: datetime,
                    max_points: int = 300) -> dict:
    """Per-GPU VRAM per poll, summed by user (bucket), for stacked charts.

    A poll with no processes on a GPU is a point with an empty `by_user`; the first point
    after a monitoring gap (a poll with dt_s NULL) carries `"gap": true`."""
    _require_polls(conn)
    lo = (now - timedelta(hours=hours)).isoformat()
    # (gpu) -> [(ts, is_gap, {bucket: mib})], one entry per poll, in ts order
    series: dict[int, list[tuple[str, bool, dict[str, int]]]] = {}
    totals: dict[int, int] = {}
    # The per-user sums are taken over the window first (proc_samples_ts), so the join never
    # walks a GPU's whole retained history; a GPU row without processes joins to NULLs.
    for ts, gpu, total, dt_s, user, n_procs, mib in conn.execute(
            "WITH s AS (SELECT ts, gpu, user, COUNT(*) AS n, SUM(used_mib) AS mib "
            "FROM proc_samples WHERE ts >= ?1 GROUP BY ts, gpu, user) "
            "SELECT g.ts, g.gpu, g.total_mib, p.dt_s, s.user, s.n, s.mib "
            "FROM gpu_samples g JOIN polls p ON p.ts = g.ts "
            "LEFT JOIN s ON s.ts = g.ts AND s.gpu = g.gpu "
            "WHERE g.ts >= ?1 ORDER BY g.gpu, g.ts", (lo,)):
        totals[gpu] = max(totals.get(gpu, 0), total)
        rows = series.setdefault(gpu, [])
        if not rows or rows[-1][0] != ts:
            rows.append((ts, dt_s is None, {}))
        if n_procs:                                  # COUNT is 0 only for a poll without processes
            rows[-1][2][UNATTRIBUTED_BUCKET if user is None else user] = mib
    gpus: dict[str, dict] = {}
    for gpu in sorted(series):
        polls = series[gpu]
        size = max(1, ceil(len(polls) / max_points))
        points = []
        for i in range(0, len(polls), size):
            chunk = polls[i:i + size]
            by_user: dict[str, float] = {}
            for _ts, _gap, users in chunk:
                for bucket, mib in users.items():
                    by_user[bucket] = by_user.get(bucket, 0) + mib
            point: dict = {"ts": chunk[0][0],
                           "by_user": {b: v / len(chunk) if size > 1 else v
                                       for b, v in by_user.items()}}
            # A gap poll at the very start of the window has nothing before it to bridge.
            if any(gap for k, (_ts, gap, _u) in enumerate(chunk) if i + k):
                point["gap"] = True
            points.append(point)
        gpus[str(gpu)] = {"total_mib": totals[gpu], "points": points}
    return {"from": lo, "to": now.isoformat(), "gpus": gpus}


def _job(ts: str, user, name, mib: int) -> dict:
    return {"pid": 0, "user": user, "name": name, "start": ts, "end": ts,
            "max_mib": mib, "ongoing": False}


def _polls_with_procs(conn: sqlite3.Connection, where: str, params: tuple, order: str):
    """Yield (ts, dt_s, [(gpu, pid, user, name, used_mib), ...]) one poll at a time."""
    cur = conn.execute(
        "SELECT p.ts, p.dt_s, s.gpu, s.pid, s.user, s.name, s.used_mib FROM polls p "
        f"LEFT JOIN proc_samples s ON s.ts = p.ts WHERE {where} ORDER BY p.ts {order}", params)
    for ts, group in groupby(cur, key=lambda r: r[0]):
        group = list(group)
        yield ts, group[0][1], [r[2:] for r in group if r[2] is not None]


def _extend_back(conn: sqlite3.Connection, gpu: int, job: dict, first_ts: str) -> None:
    """Move job['start'] back to the run's true first poll (it is open at `first_ts`)."""
    pid = job["pid"]
    # A run never reaches back past the latest gap poll (dt_s NULL) before `first_ts`.
    gap = conn.execute("SELECT MAX(ts) FROM polls WHERE dt_s IS NULL AND ts < ?",
                       (first_ts,)).fetchone()[0]
    floor = gap if gap is not None else conn.execute("SELECT MIN(ts) FROM polls").fetchone()[0]
    # Latest poll in [floor, first_ts) that lacks the pid: ordered index scans and a set
    # difference, over a look-back range that grows 4x per step so short runs stay cheap.
    first_dt = datetime.fromisoformat(first_ts)
    width = timedelta(hours=1)
    while True:
        lo = max(floor, (first_dt - width).isoformat())
        n_polls, n_seen = conn.execute(
            "SELECT (SELECT COUNT(*) FROM polls WHERE ts >= ?1 AND ts < ?2), "
            "(SELECT COUNT(*) FROM proc_samples "
            "WHERE gpu = ?3 AND pid = ?4 AND ts >= ?1 AND ts < ?2)",
            (lo, first_ts, gpu, pid)).fetchone()
        missing = None if n_polls == n_seen else conn.execute(
            "SELECT MAX(ts) FROM (SELECT ts FROM polls WHERE ts >= ?1 AND ts < ?2 "
            "EXCEPT SELECT ts FROM proc_samples "
            "WHERE gpu = ?3 AND pid = ?4 AND ts >= ?1 AND ts < ?2)",
            (lo, first_ts, gpu, pid)).fetchone()[0]
        if missing is not None or lo == floor:
            break
        width *= 4
    if missing is None:   # contiguous back to the floor: the gap poll (or first poll) starts it
        start = floor
    else:
        start = conn.execute("SELECT MIN(ts) FROM polls WHERE ts > ?", (missing,)).fetchone()[0]
    if start < first_ts:
        top = conn.execute("SELECT MAX(used_mib) FROM proc_samples "
                           "WHERE gpu = ? AND pid = ? AND ts >= ? AND ts < ?",
                           (gpu, pid, start, first_ts)).fetchone()[0]
        job["start"] = start
        job["max_mib"] = max(job["max_mib"], top or 0)


def timeline(conn: sqlite3.Connection, hours: int, now: datetime) -> dict:
    """Runs of consecutive polls per (gpu, pid), split at monitoring gaps (dt_s NULL)."""
    _require_polls(conn)
    lo, hi = (now - timedelta(hours=hours)).isoformat(), now.isoformat()
    last_poll = conn.execute("SELECT MAX(ts) FROM polls").fetchone()[0]
    gpu_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT gpu FROM gpu_samples WHERE ts >= ? AND ts <= ?", (lo, hi))}
    open_runs: dict[tuple[int, int], dict] = {}
    done: list[tuple[int, dict]] = []
    first_ts = first_dt = None

    def close(keys):
        for k in keys:
            job = open_runs.pop(k)
            job["ongoing"] = job["end"] == last_poll
            done.append((k[0], job))

    for ts, dt_s, procs in _polls_with_procs(conn, "p.ts >= ? AND p.ts <= ?", (lo, hi), "ASC"):
        if first_ts is None:
            first_ts, first_dt = ts, dt_s
        if dt_s is None:
            close(list(open_runs))
        present = {(gpu, pid): (user, name, mib) for gpu, pid, user, name, mib in procs}
        close([k for k in open_runs if k not in present])
        for k, (user, name, mib) in present.items():
            gpu_ids.add(k[0])
            job = open_runs.get(k)
            if job is None:
                job = open_runs[k] = _job(ts, user, name, mib)
                job["pid"] = k[1]
            job["end"] = ts
            job["max_mib"] = max(job["max_mib"], mib)
            job["user"], job["name"] = user, name
    close(list(open_runs))

    # Runs open at the first in-window poll may have begun earlier: find each true start in SQL.
    if first_ts is not None and first_dt is not None:
        for _g, job in done:
            if job["start"] == first_ts:
                _extend_back(conn, _g, job, first_ts)

    gpus: dict[str, list[dict]] = {str(g): [] for g in sorted(gpu_ids)}
    for g, job in sorted(done, key=lambda gj: (gj[1]["start"], gj[0], gj[1]["pid"])):
        gpus[str(g)].append(job)
    return {"from": lo, "to": hi, "gpus": gpus, "box": _box_runs(conn, lo, hi, last_poll)}


BOX_CPU_MIN_CORES = 1.0
BOX_RAM_MIN_MIB = 64 * 1024


def _box_runs(conn: sqlite3.Connection, lo: str, hi: str, last_poll: str) -> dict:
    """Per user, runs of consecutive polls at or above the CPU or the RAM threshold, split
    at monitoring gaps (dt_s NULL). A run open at the window's start is cut there."""
    out = {"cpu": [], "ram": [], "cpu_min_cores": BOX_CPU_MIN_CORES,
           "ram_min_mib": BOX_RAM_MIN_MIB}
    try:
        cur = conn.execute(
            "SELECT p.ts, p.dt_s, u.user, u.cores, u.rss_mib FROM polls p "
            "LEFT JOIN user_samples u ON u.ts = p.ts WHERE p.ts >= ? AND p.ts <= ? "
            "ORDER BY p.ts", (lo, hi))
    except sqlite3.OperationalError:            # written by a monitor without CPU/RAM
        return out
    open_runs: dict[str, dict[str, dict]] = {"cpu": {}, "ram": {}}

    def close(metric: str, users) -> None:
        for u in users:
            run = open_runs[metric].pop(u)
            run["ongoing"] = run["end"] == last_poll
            out[metric].append(run)

    for ts, group in groupby(cur, key=lambda r: r[0]):
        group = list(group)
        if group[0][1] is None:
            for m in open_runs:
                close(m, list(open_runs[m]))
        present = {
            "cpu": {u: c for _, _, u, c, _ in group
                    if u is not None and c is not None and c >= BOX_CPU_MIN_CORES},
            "ram": {u: r for _, _, u, _, r in group if u is not None and r >= BOX_RAM_MIN_MIB},
        }
        for m, now_in in present.items():
            close(m, [u for u in open_runs[m] if u not in now_in])
            for u, v in now_in.items():
                run = open_runs[m].setdefault(
                    u, {"user": u, "start": ts, "end": ts, "peak": v, "ongoing": False})
                run["end"], run["peak"] = ts, max(run["peak"], v)
    for m in open_runs:
        close(m, list(open_runs[m]))
    for m in ("cpu", "ram"):
        out[m].sort(key=lambda r: (r["start"], r["user"]))
    return out
