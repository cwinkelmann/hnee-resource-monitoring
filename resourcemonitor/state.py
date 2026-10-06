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
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = {}
            return cls(path, data)
        except (OSError, ValueError):       # incl. JSONDecodeError, UnicodeDecodeError
            # A corrupt file must not take the daemon down; the cost is at most one
            # duplicate alert, which is far cheaper than a monitor that is not running.
            return cls(path, {})

    def should_send(self, key: str, now: datetime, cooldown_s: int) -> bool:
        last = self._sent.get(key)
        if last is not None:
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds()
            except (TypeError, ValueError):
                age = None          # unparsable or naive timestamp: treat as not seen
            if age is not None and age < cooldown_s:
                return False
        self._sent[key] = now.isoformat()
        return True

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._sent, indent=1))
        tmp.replace(self._path)            # atomic; a killed daemon cannot truncate it
