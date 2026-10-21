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

from btlog import notify
from btlog.decoders import UNITS
from btlog.rules import RULES
from btlog.store import KEEP

DEFAULT_PATH = "/etc/btlog.toml"

# alerts names the rules of `btlog alerts` (see rules.py); "away" opens when a
# labelled device leaves and closes when it is back.
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
    alerts: list = field(default_factory=lambda: ["silent", "away"])  # rule names, for `btlog alerts`
    notify: str | None = None     # where alerts go: ntfy URL, webhook:URL or command:PROGRAM (notify.py)
    silent_hours: float = 6.0     # a labelled sensor unheard this long is silent
    away_minutes: float = 15.0    # a labelled device unheard this long has left; the debounce of away
    range: dict = field(default_factory=dict)   # label -> {kind: [low, high]}, for the range rule
    web_bind: str = "127.0.0.1:8778"
    web_names: list = field(default_factory=list)  # extra Host names for the dashboard, e.g. Tailscale's


def load(path=None):
    """The config at path, $BTLOG_CONFIG or /etc/btlog.toml; defaults if the default file is missing."""
    given = path or os.environ.get("BTLOG_CONFIG")
    try:
        # utf-8-sig: Windows editors and PowerShell's -Encoding utf8 start the
        # file with a BOM, which tomllib reads as a stray character.
        data = tomllib.loads(Path(given or DEFAULT_PATH).read_text(encoding="utf-8-sig"))
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
    numbers = {"interval": "positive", "retention_days": "0 or more", "forget_random_days": "0 or more",
               "silent_hours": "positive", "away_minutes": "positive"}
    choices = {"mode": MODES, "keep": KEEP}
    for key, value in data.items():
        if key in numbers:
            ok = _number(value) and (value > 0 if numbers[key] == "positive" else value >= 0)
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
            if key == "alerts" and not set(value) <= set(RULES):
                raise ValueError(f"config key alerts: unknown rule(s) {', '.join(sorted(set(value) - set(RULES)))}; "
                                 f"known: {', '.join(RULES)}")
        elif key == "range":
            _check_range(value)
        elif not isinstance(value, str):
            raise ValueError(f"config key {key} must be a string")
        elif key == "notify":
            notify.check(value)
    if "db" in data:
        data["db"] = Path(data["db"]).expanduser()
    # A copy, so that one Config appending to its alerts cannot change the mode's.
    return Config(**(copy.deepcopy(MODES[data.get("mode", "home")]) | data))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_range(table):
    """label -> kind -> [low, high], as `[range.freezer] temperature = [-30, -15]`
    gives it. Checked to the last number: a misspelt kind or swapped bounds
    would otherwise watch nothing, and nobody notices until the freezer has
    thawed."""
    if not isinstance(table, dict):
        raise ValueError("config key range must be a table per device label, e.g. [range.freezer]")
    for label, kinds in table.items():
        if not isinstance(kinds, dict):
            raise ValueError(f"config range.{label} must be a table of kind = [low, high]")
        for kind, bounds in kinds.items():
            if kind not in UNITS:
                raise ValueError(f"config range.{label}: unknown kind {kind!r}; known: {', '.join(UNITS)}")
            if not (isinstance(bounds, list) and len(bounds) == 2 and all(map(_number, bounds))
                    and bounds[0] <= bounds[1]):
                raise ValueError(f"config range.{label}.{kind} must be [low, high], two numbers, low <= high")
