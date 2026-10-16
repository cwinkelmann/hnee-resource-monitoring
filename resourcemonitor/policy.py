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
    grace_minutes: int = 30        # notice an important booking gives a running lendable one
    max_pending_per_user: int = 16  # future bookings one user may hold at once
    max_watchers: int = 64          # parked /events connections, ~2x carrot's slot count


def _bounded(raw: dict, key: str, default: int, lo: int, hi: int) -> int:
    """A whole number from [booking], or the default. Bad policy fails at load time."""
    v = raw.get("booking", {}).get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"booking.{key} must be a whole number, {lo}..{hi}")
    return v


def load_policy(path: Path | str) -> Policy:
    raw = tomllib.loads(Path(path).read_text())
    for section in ("rules", "notify"):
        if section not in raw:
            raise ValueError(f"policy is missing the [{section}] section")

    assignments: dict[str, frozenset[int]] = {}
    seen: dict[int, str] = {}
    for user, gpus in raw.get("assignments", {}).items():
        for g in gpus:
            if not isinstance(g, int) or not 0 <= g <= MAX_GPU_INDEX:
                raise ValueError(f"{user}: {g} is not a GPU index in 0..{MAX_GPU_INDEX}")
            if g in seen:
                # Otherwise "is this user out of allocation" has two answers.
                raise ValueError(f"{seen[g]} and {user} are both assigned GPU {g}")
            seen[g] = user
        assignments[user] = frozenset(gpus)

    r, n = raw["rules"], raw["notify"]
    grace = raw.get("booking", {}).get("grace_minutes", 30)
    if not isinstance(grace, int) or isinstance(grace, bool) or not 0 <= grace <= 24 * 60:
        raise ValueError("booking.grace_minutes must be a whole number of minutes, 0..1440")
    return Policy(
        assignments=assignments,
        idle_util_pct=int(r["idle_util_pct"]),
        idle_min_mib=int(r["idle_min_mib"]),
        idle_grace_s=int(r["idle_grace_s"]),
        capacity_free_mib=int(r["capacity_free_mib"]),
        cooldown_s=int(n["cooldown_s"]),
        channel=str(n["channel"]),
        grace_minutes=grace,
        max_pending_per_user=_bounded(raw, "max_pending_per_user", 16, 1, 10_000),
        max_watchers=_bounded(raw, "max_watchers", 64, 1, 4_096),
    )
