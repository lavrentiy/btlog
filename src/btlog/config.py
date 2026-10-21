"""Where the database is and how the scanner behaves.

A TOML file, /etc/btlog.toml by default (what the NixOS module writes), or
$BTLOG_CONFIG, or --config. Every key has a default, so `btlog daemon` with no
file at all logs into ~/.local/state/btlog/btlog.db; the CLI and the dashboard
read the same file so they find the same database as the daemon.
"""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = "/etc/btlog.toml"


def _default_db():
    return Path("~/.local/state/btlog/btlog.db").expanduser()


@dataclass
class Config:
    db: Path = field(default_factory=_default_db)
    interval: float = 60.0        # seconds per scan cycle: one sighting per device per cycle
    adapter: str | None = None    # "hci0"; None picks the default adapter
    retention_days: int = 90      # raw sightings; hourly rollups are kept forever
    forget_random_days: int = 7   # private random addresses unseen this long are removed
    web_bind: str = "127.0.0.1:8778"


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
    for key, kinds in {"interval": (int, float), "retention_days": (int,), "forget_random_days": (int,),
                       "adapter": (str, type(None)), "web_bind": (str,), "db": (str,)}.items():
        if key in data and (not isinstance(data[key], kinds) or isinstance(data[key], bool)):
            raise ValueError(f"config key {key} must be {' or '.join(k.__name__ for k in kinds)}")
        if key in data and kinds[0] in (int, float) and data[key] <= 0:
            raise ValueError(f"config key {key} must be positive")
    if "db" in data:
        data["db"] = Path(data["db"]).expanduser()
    return Config(**data)
