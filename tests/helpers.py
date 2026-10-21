"""Shared by the tests: a scripted scanner source and a clock that steps.

Times are built from local naive datetimes, so the hourly rollups land in the
expected local hour whatever zone the test machine is in.
"""

from datetime import datetime, timedelta

from btlog.scanner import Advert

PHONE = "5A:11:22:33:44:55"     # 0x5A = 01.. : resolvable private address
WATCH = "C0:AA:BB:CC:DD:EE"     # 0xC0 = 11.. : static random
BEACON = "00:11:22:33:44:55"    # public
NRPA = "3F:00:00:00:00:01"      # 0x3F = 00.. : non-resolvable private


def at(day, hour, minute=0):
    """Unix seconds of a local time in October 2026; day is the day of month."""
    return datetime(2026, 10, day, hour, minute).timestamp()


def weekday(day):
    return datetime(2026, 10, day).weekday()


def adverts(*specs):
    """Advert list from (address, rssi[, name]) tuples; random kinds set from the address."""
    out = []
    for spec in specs:
        address, rssi, *rest = spec
        kind = "public" if address == BEACON else "random"
        out.append(Advert(address, rssi, address_type=kind, name=rest[0] if rest else None,
                          manufacturer=rest[1] if len(rest) > 1 else None))
    return out


class ScriptedSource:
    """Returns one scripted cycle per scan() call, then None. An item that is an
    exception is raised instead, to exercise the retry path."""

    def __init__(self, cycles):
        self.cycles = list(cycles)
        self.asked = []

    def scan(self, seconds):
        self.asked.append(seconds)
        if not self.cycles:
            return None
        item = self.cycles.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    """A clock that advances by step on every call, like the real one over cycles."""

    def __init__(self, start, step=60):
        self.now, self.step = start, step
        self.slept = []

    def __call__(self):
        t = self.now
        self.now += self.step
        return t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def fill_day(store, day, present, interval_minutes=10, rssi=-60):
    """One cycle every interval_minutes through a whole local day; present maps
    an address to the set of hours it is heard in."""
    for hour in range(24):
        for minute in range(0, 60, interval_minutes):
            heard = [(a, rssi) for a, hours in present.items() if hour in hours]
            store.record_cycle(at(day, hour, minute), adverts(*heard))
    return 60 // interval_minutes


def days_in_week(weekday_wanted):
    """Days of October 2026 that fall on the given weekday."""
    return [d for d in range(1, 32) if (datetime(2026, 10, 1) + timedelta(days=d - 1)).weekday() == weekday_wanted]
