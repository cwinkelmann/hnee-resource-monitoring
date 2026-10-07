"""The only module that writes history. The web process reads it with mode=ro."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from resourcemonitor.energy import EnergyRow
from resourcemonitor.host import HostUsage
from resourcemonitor.model import Snapshot
from resourcemonitor.rules import Alert

SCHEMA_VERSION = 2               # 2: host_samples and user_samples (CPU and RAM)

_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS host_samples (ts TEXT NOT NULL, ncpu INTEGER NOT NULL,
  cores_busy REAL, load1 REAL NOT NULL, mem_total_mib INTEGER NOT NULL,
  mem_used_mib INTEGER NOT NULL, swap_total_mib INTEGER NOT NULL,
  swap_used_mib INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_samples (ts TEXT NOT NULL, user TEXT NOT NULL, cores REAL,
  rss_mib INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS host_samples_ts ON host_samples(ts);
CREATE INDEX IF NOT EXISTS user_samples_ts ON user_samples(ts);
CREATE INDEX IF NOT EXISTS gpu_samples_ts ON gpu_samples(ts);
CREATE INDEX IF NOT EXISTS proc_samples_ts ON proc_samples(ts);
CREATE INDEX IF NOT EXISTS proc_samples_gpu_pid_ts ON proc_samples(gpu, pid, ts);
CREATE INDEX IF NOT EXISTS polls_gap ON polls(ts) WHERE dt_s IS NULL;
CREATE INDEX IF NOT EXISTS energy_samples_ts ON energy_samples(ts);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
"""

_TS_TABLES = ("gpu_samples", "proc_samples", "energy_samples", "alerts", "host_samples",
              "user_samples")
SLACK_MODES = ("dry-run", "posting")


class HistoryWriter:
    def __init__(self, path: Path | str, retention_days: int = 90,
                 slack_mode: str | None = None):
        """slack_mode ('dry-run' | 'posting') is stored in meta so the page can say
        whether alerts actually reach Slack; None leaves any stored value alone."""
        if slack_mode is not None and slack_mode not in SLACK_MODES:
            raise ValueError(f"slack_mode must be one of {SLACK_MODES}, not {slack_mode!r}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._last_prune: date | None = None
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if slack_mode is not None:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('slack', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (slack_mode,))

    def record(self, snap: Snapshot, dt_s: float | None, energy: list[EnergyRow],
               alerts: list[Alert], sent_keys: set[str], box: HostUsage | None = None) -> None:
        ts = snap.taken_at.isoformat()
        with self._conn:
            self._conn.execute("INSERT INTO polls (ts, dt_s) VALUES (?, ?)", (ts, dt_s))
            self._conn.executemany(
                "INSERT INTO gpu_samples VALUES (?, ?, ?, ?, ?, ?)",
                [(ts, g.index, g.total_mib, g.used_mib, g.util_pct, g.power_w) for g in snap.gpus])
            self._conn.executemany(
                "INSERT INTO proc_samples VALUES (?, ?, ?, ?, ?, ?)",
                [(ts, p.gpu_index, p.pid, p.user, p.used_mib, p.name) for p in snap.procs])
            self._conn.executemany(
                "INSERT INTO energy_samples VALUES (?, ?, ?, ?, ?)",
                [(ts, r.gpu, r.bucket, r.kwh, r.seconds) for r in energy])
            self._conn.executemany(
                "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(ts, a.kind, a.key, a.gpu_index, a.user, a.text,
                  1 if a.key in sent_keys else 0) for a in alerts])
            if box is not None:
                self._conn.execute(
                    "INSERT INTO host_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, box.ncpu, box.cores_busy, box.load1, box.mem_total_mib,
                     box.mem_used_mib, box.swap_total_mib, box.swap_used_mib))
                self._conn.executemany(
                    "INSERT INTO user_samples VALUES (?, ?, ?, ?)",
                    [(ts, u.user, None if u.cores is None else round(u.cores, 2), u.rss_mib)
                     for u in box.users])
        day = snap.taken_at.date()
        if day != self._last_prune:
            self.prune(snap.taken_at)
            self._last_prune = day

    def prune(self, now: datetime) -> int:
        cutoff = (now - timedelta(days=self.retention_days)).isoformat()
        with self._conn:
            for table in _TS_TABLES:
                self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            return self._conn.execute("DELETE FROM polls WHERE ts < ?", (cutoff,)).rowcount

    def close(self) -> None:
        self._conn.close()
