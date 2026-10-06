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
