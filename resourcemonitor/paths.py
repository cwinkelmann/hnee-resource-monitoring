"""Default file locations, shared by the monitor (cli) and the dashboard (web).

Kept apart so the web process can know them without importing cli (and with it
probe and notify).
"""
from pathlib import Path

DEFAULT_POLICY = Path.home() / ".config/resourcemonitor/policy.toml"
DEFAULT_STATE = Path.home() / ".local/state/resourcemonitor/state.json"
DEFAULT_ENERGY = Path.home() / ".local/state/resourcemonitor/energy.json"
DEFAULT_HISTORY = Path.home() / ".local/state/resourcemonitor/history.sqlite"
