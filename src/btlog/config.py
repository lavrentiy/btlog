"""Where the database is and how the scanner behaves.

A TOML file, /etc/btlog.toml by default (what the NixOS module writes), or
$BTLOG_CONFIG, or --config. Every key has a default, so `btlog daemon` with no
file at all logs into ~/.local/state/btlog/btlog.db; the CLI and the dashboard
read the same file so they find the same database as the daemon.

A mode is a named set of defaults for some of the keys, not a code path of its
own: `mode = "sensors"` means the same as writing the keys it implies, and any
key written in the file still wins. So the tests cover the keys, and a mode is
one line for the user. Config() itself is the home mode, today's behaviour;
load() is what applies another mode.
"""

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from btlog.store import KEEP

DEFAULT_PATH = "/etc/btlog.toml"

# alerts names the rules for `btlog alerts`, which is still to come
# (docs/roadmap.md, phase 3); nothing reads it yet.
MODES = {
    "home":     {"keep": "all", "payloads": False, "retention_days": 90, "forget_random_days": 7,
                 "alerts": ["silent", "away"]},
    "sensors":  {"keep": "sensors", "payloads": False, "retention_days": 90, "forget_random_days": 7,
                 "alerts": ["silent", "range"]},
    "footfall": {"keep": "counts", "payloads": False, "retention_days": 30, "forget_random_days": 7,
                 "alerts": []},
    "record":   {"keep": "all", "payloads": True, "retention_days": 0, "forget_random_days": 0,
                 "alerts": []},
}


def _default_db():
    return Path("~/.local/state/btlog/btlog.db").expanduser()


@dataclass
class Config:
    db: Path = field(default_factory=_default_db)
    interval: float = 60.0        # seconds per scan cycle: one sighting per device per cycle
    adapter: str | None = None    # "hci0"; None picks the default adapter
    mode: str = "home"            # the defaults below are home's; see MODES
    keep: str = "all"             # all, sensors (decoded sensors only) or counts (no rotating address stored)
    payloads: bool = False        # raw manufacturer and service data too, for evidence and re-decoding
    retention_days: int = 90      # raw sightings, readings and payloads; 0 keeps them forever (rollups always are)
    forget_random_days: int = 7   # private random addresses unseen this long are removed; 0 never
    alerts: list = field(default_factory=lambda: ["silent", "away"])  # rule names; nothing reads them yet
    web_bind: str = "127.0.0.1:8778"
    web_names: list = field(default_factory=list)  # extra Host names for the dashboard, e.g. Tailscale's


def load(path=None):
    """The config at path, $BTLOG_CONFIG or /etc/btlog.toml; defaults if the default file is missing."""
    given = path or os.environ.get("BTLOG_CONFIG")
    try:
        data = tomllib.loads(Path(given or DEFAULT_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if given:
            raise
        data = {}
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown config key(s): {', '.join(unknown)}")
    # TOML has types, so "60" (a string) is a mistake worth stopping on now
    # rather than an hour later inside the purge.
    numbers = {"interval": "positive", "retention_days": "0 or more", "forget_random_days": "0 or more"}
    choices = {"mode": MODES, "keep": KEEP}
    for key, value in data.items():
        if key in numbers:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and (
                value > 0 if numbers[key] == "positive" else value >= 0)
            if not ok:
                raise ValueError(f"config key {key} must be a number, {numbers[key]}")
        elif key in choices:
            if not (isinstance(value, str) and value in choices[key]):
                raise ValueError(f"config key {key} must be one of {', '.join(choices[key])}")
        elif key == "payloads":
            if not isinstance(value, bool):
                raise ValueError("config key payloads must be true or false")
        elif key in ("web_names", "alerts"):
            if not (isinstance(value, list) and all(isinstance(n, str) for n in value)):
                raise ValueError(f"config key {key} must be a list of strings")
        elif not isinstance(value, str):
            raise ValueError(f"config key {key} must be a string")
    if "db" in data:
        data["db"] = Path(data["db"]).expanduser()
    # A copy, so that one Config appending to its alerts cannot change the mode's.
    return Config(**(copy.deepcopy(MODES[data.get("mode", "home")]) | data))
