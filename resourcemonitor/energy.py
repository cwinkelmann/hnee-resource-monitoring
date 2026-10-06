"""Energy = sum(power x dt) over our own polls. This driver has no energy counter.

Everything here is an estimate with a stated shape:
  * per GPU   -- as good as nvidia-smi's power.draw and the poll interval
  * per user  -- power is per CARD, so several processes on one card are split by
                 memory share. That is a proxy, not a measurement, and the report says so.
  * idle      -- power drawn by a card with no compute process on it. Nobody is billed.
  * unattributed -- a holder whose owner could not be resolved; kept apart, never guessed.
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
    unattributed_kwh: float = 0.0
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
                share = max(p.used_mib, 1) / total_mib
                if p.user is None:                  # unattributed: do not bill a guess,
                    self.unattributed_kwh += kwh * share   # but do not lose it either
                    continue
                self.per_user_kwh[p.user] = self.per_user_kwh.get(p.user, 0.0) + kwh * share

    def totals(self) -> dict:
        return {"per_gpu": dict(self.per_gpu_kwh), "per_user": dict(self.per_user_kwh),
                "idle_kwh": self.idle_kwh,
                "unattributed_kwh": self.unattributed_kwh, "since": self.since}

    @classmethod
    def load(cls, path: Path | str, max_gap_s: int = 300) -> "EnergyLedger":
        try:
            d = json.loads(Path(path).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls(max_gap_s=max_gap_s)
        led = cls(max_gap_s=max_gap_s, idle_kwh=d.get("idle_kwh", 0.0),
                  unattributed_kwh=d.get("unattributed_kwh", 0.0),
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
    unattr = totals.get("unattributed_kwh", 0.0)
    lines.append(f"• _unattributed (owner unknown): {unattr:.1f} kWh "
                 f"(~€{unattr * price_per_kwh:.2f})_")
    gpu_total = sum(totals.get("per_gpu", {}).values())
    lines.append(f"• _all cards total: {gpu_total:.1f} kWh (~€{gpu_total * price_per_kwh:.2f})_")
    lines.append("")
    lines.append("Per-user figures split a card's draw by memory share when several "
                 "processes share it — an estimate, not a measurement.")
    return "\n".join(lines)
