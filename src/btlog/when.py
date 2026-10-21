"""Times on the command line: '30m', '2h', '3d', 'today', 'yesterday',
'2026-10-18', '2026-10-18T19:30' or 'all'.

parse() gives unix seconds, the unit the sightings use (None for 'all'); day()
turns that into the local 'YYYY-MM-DD' the rollup tables are keyed on.
"""

import re
from datetime import datetime, time, timedelta

_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse(text, now=None):
    """A point in time as unix seconds; relative amounts count back from now."""
    now = datetime.now() if now is None else datetime.fromtimestamp(now)
    s = text.strip().lower()
    if s in ("all", "ever"):
        return None
    if s == "now":
        return now.timestamp()
    if s == "today":
        return midnight(now).timestamp()
    if s == "yesterday":
        return midnight(now - timedelta(days=1)).timestamp()
    m = _REL.match(s)
    if m:
        return now.timestamp() - float(m.group(1)) * _UNIT[m.group(2)]
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        raise ValueError(
            f"can't read time {text!r}: use e.g. 30m, 2h, 3d, today, 2026-10-18 or 2026-10-18T19:30"
        ) from None


def midnight(dt):
    return datetime.combine(dt.date(), time())


def day(ts):
    """Local 'YYYY-MM-DD' of unix seconds; '' (before every day) for None."""
    return "" if ts is None else datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def fmt(ts, seconds=False):
    """Local 'YYYY-MM-DD HH:MM' of unix seconds, '-' for None."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
