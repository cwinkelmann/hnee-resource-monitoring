"""Pure read-only queries over the history database. No HTTP, no clock except `now`."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from math import ceil
from pathlib import Path

CAVEATS = [
    "Measured only while the monitor was running.",
    "Per-user kWh splits a card's draw by memory share when several processes share it "
    "— an estimate, not a measurement.",
]


class NoHistory(Exception):
    """The history file is missing or holds no polls yet."""


def open_ro(path: Path | str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise NoHistory(str(path))
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def latest(conn: sqlite3.Connection, assignments: dict[str, frozenset[int]],
           now: datetime, stale_after_s: int) -> dict:
    ts = conn.execute("SELECT MAX(ts) FROM polls").fetchone()[0]
    if ts is None:
        raise NoHistory("no polls")
    age_s = (now - datetime.fromisoformat(ts)).total_seconds()
    owner = {g: user for user, gpus in assignments.items() for g in gpus}
    procs: dict[int, list[dict]] = {}
    for gpu, pid, user, name, used in conn.execute(
            "SELECT gpu, pid, user, name, used_mib FROM proc_samples WHERE ts=? "
            "ORDER BY used_mib DESC, pid", (ts,)):
        procs.setdefault(gpu, []).append(
            {"pid": pid, "user": user, "name": name, "used_mib": used})
    gpus = [{"gpu": g, "total_mib": total, "used_mib": used, "util_pct": util,
             "power_w": power, "assigned_to": owner.get(g), "procs": procs.get(g, [])}
            for g, total, used, util, power in conn.execute(
                "SELECT gpu, total_mib, used_mib, util_pct, power_w FROM gpu_samples "
                "WHERE ts=? ORDER BY gpu", (ts,))]
    alerts = [{"kind": k, "key": key, "gpu": gpu, "user": user, "text": text, "sent": bool(sent)}
              for k, key, gpu, user, text, sent in conn.execute(
                  "SELECT kind, key, gpu, user, text, sent FROM alerts WHERE ts=? "
                  "ORDER BY rowid", (ts,))]
    return {"ts": ts, "age_s": age_s, "stale": age_s > stale_after_s,
            "gpus": gpus, "alerts": alerts}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def usage(conn: sqlite3.Connection, start: date, end: date, by: str, now: datetime) -> dict:
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
    monitored_s, first = conn.execute(
        "SELECT COALESCE(SUM(dt_s), 0), MIN(ts) FROM polls WHERE ts >= ? AND ts < ?",
        (lo, hi)).fetchone()
    elapsed_s = 0.0
    first_any = conn.execute("SELECT MIN(ts) FROM polls").fetchone()[0]
    if first_any is not None:
        t0 = max(datetime.combine(start, datetime.min.time(), timezone.utc),
                 datetime.fromisoformat(first_any))
        t1 = min(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc), now)
        elapsed_s = max(0.0, (t1 - t0).total_seconds())
    ratio = monitored_s / elapsed_s if elapsed_s else 0.0
    return {"from": start.isoformat(), "to": end.isoformat(), "by": by,
            "periods": list(periods.values()), "totals": totals,
            "coverage": {"monitored_s": float(monitored_s), "elapsed_s": elapsed_s, "ratio": ratio},
            "caveats": list(CAVEATS)}


def timeseries(conn: sqlite3.Connection, hours: int, now: datetime, max_points: int = 300) -> dict:
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


def timeline(conn: sqlite3.Connection, hours: int, now: datetime) -> dict:
    """Runs of consecutive polls per (gpu, pid), split at monitoring gaps (dt_s NULL)."""
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

    # Runs open at the first in-window poll may have begun earlier: walk backwards.
    if first_ts is not None and first_dt is not None:
        pending = {(g, j["pid"]): j for g, j in done if j["start"] == first_ts}
        if pending:
            for ts, dt_s, procs in _polls_with_procs(conn, "p.ts < ?", (first_ts,), "DESC"):
                present = {(gpu, pid): mib for gpu, pid, _u, _n, mib in procs}
                for k in [k for k in pending if k not in present]:
                    del pending[k]
                for k, job in pending.items():
                    job["start"] = ts
                    job["max_mib"] = max(job["max_mib"], present[k])
                if dt_s is None or not pending:
                    break

    gpus: dict[str, list[dict]] = {str(g): [] for g in sorted(gpu_ids)}
    for g, job in sorted(done, key=lambda gj: (gj[1]["start"], gj[0], gj[1]["pid"])):
        gpus[str(g)].append(job)
    return {"from": lo, "to": hi, "gpus": gpus}
