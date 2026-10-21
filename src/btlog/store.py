"""SQLite storage: every sighting, plus hourly rollups that outlive the raw rows.

  devices    one row per address: kind, latest name, optional label, first/last seen.
  sightings  one row per device per scan cycle, with the strongest RSSI heard in
             that cycle. Deleted after retention_days; this is the bulk of the file.
  hourly     per device per local hour: cycles heard, best and summed RSSI. Tiny
             and kept forever, so hour-of-day and weekday trends work over years.
  cycles     per local hour: how many scan cycles the daemon ran. Presence is a
             fraction of that rather than of the wall clock, so a day the scanner
             was off does not read as a day nobody was home.
  readings   one row per sensor per kind (temperature, humidity...) per cycle it
             was heard in, decoded from its advertisements. Deleted with the
             sightings after retention_days.
  hourly_readings  per sensor per kind per local hour: count, min, max and sum,
             kept forever like hourly.

Hours are local time, because "who is here at eight in the morning" is a local
question; day and weekday are stored alongside so GROUP BY stays in SQL.

Private random addresses (resolvable and non-resolvable) rotate every few
minutes by design. One that has not been seen for forget_random_days is a dead
end for trends and only a privacy liability, so purge() removes it; its hourly
rows are folded into a single "forgotten" pseudo-device first, so the count of
devices around per hour stays right after the address is gone.

PRAGMA user_version says which tables exist: 1 (or 0, never set) is the schema
without readings, 2 adds them. A writable Store migrates forward on open; a
read-only one on an older file (the dashboard upgraded before the daemon was
restarted) answers the reading queries with nothing rather than failing.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from btlog import decoders, vendors

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,             -- public, static, rpa, nrpa, unknown, forgotten
    name TEXT,
    label TEXT,
    manufacturer INTEGER,           -- Bluetooth SIG company id from manufacturer data
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    cycles INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
    ts REAL NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    rssi INTEGER NOT NULL,
    adverts INTEGER NOT NULL,       -- reports from the Bluetooth stack in the cycle
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE TABLE IF NOT EXISTS hourly (
    day TEXT NOT NULL,              -- YYYY-MM-DD, local
    hour INTEGER NOT NULL,          -- 0..23, local
    weekday INTEGER NOT NULL,       -- 0 = Monday
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    cycles INTEGER NOT NULL,
    rssi_max INTEGER NOT NULL,
    rssi_sum INTEGER NOT NULL,
    PRIMARY KEY (device_id, day, hour)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS hourly_hour ON hourly(day, hour);  -- the all-device queries
CREATE TABLE IF NOT EXISTS cycles (
    day TEXT NOT NULL,
    hour INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    count INTEGER NOT NULL,
    last_ts REAL NOT NULL,          -- start of the latest cycle in this hour
    PRIMARY KEY (day, hour)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS readings (
    ts REAL NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,             -- temperature, humidity, battery... (decoders.UNITS)
    value REAL NOT NULL,            -- the last one heard in the cycle
    PRIMARY KEY (device_id, kind, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly_readings (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    hour INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    kind TEXT NOT NULL,
    n INTEGER NOT NULL,             -- cycles with a reading of this kind
    min REAL NOT NULL,
    max REAL NOT NULL,
    sum REAL NOT NULL,
    PRIMARY KEY (device_id, kind, day, hour)
) WITHOUT ROWID;
"""
VERSION = 2   # PRAGMA user_version of SCHEMA; see the module docstring

PRIVATE_KINDS = ("rpa", "nrpa")
FORGOTTEN = "forgotten"


def address_kind(address, address_type=None):
    """public, static, rpa, nrpa or unknown.

    A random address carries its sub-kind in the top two bits: 11 static (fixed
    for the device's lifetime, as good as public), 01 resolvable private (rotates,
    phones and watches), 00 non-resolvable private. Backends that do not report
    the address type (bleak on Windows) give unknown, which is kept like public.
    """
    if address_type == "public":
        return "public"
    if address_type != "random":
        return "unknown"
    try:
        top = int(address[:2], 16) >> 6
    except ValueError:
        return "unknown"
    return {3: "static", 1: "rpa", 0: "nrpa"}.get(top, "unknown")


def local_hour(ts):
    """(day 'YYYY-MM-DD', hour, weekday) of a unix time in the machine's zone."""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday()


def between(since, until, table=""):
    """SQL condition and parameters for rollup rows from since up to (not
    including) until, to the hour. The rollups are keyed on (day, hour), so
    '--since 2h' must not pull in the whole of today. None means open-ended."""
    t = f"{table}." if table else ""
    conds, args = [], []
    if since is not None:
        day, hour, _ = local_hour(since)
        conds.append(f"({t}day > ? OR ({t}day = ? AND {t}hour >= ?))")
        args += [day, day, hour]
    if until is not None:
        day, hour, _ = local_hour(until)
        conds.append(f"({t}day < ? OR ({t}day = ? AND {t}hour < ?))")
        args += [day, day, hour]
    return " AND ".join(conds) or "1", tuple(args)


def printable(text):
    """A device name with control characters removed, or None if nothing is
    left. Names come from strangers' advertisements and go to terminals, so an
    escape sequence that clears the screen or retitles the window must not
    get through."""
    if not text:
        return None
    return "".join(c for c in text if c.isprintable()) or None


def _row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _fold(text):
    """Case folding for SQL: SQLite's own upper() only knows ASCII, so "Zoë"
    would never match "zoë" without this."""
    return text.casefold() if text is not None else None


def public(d):
    """A device row for JSON output: times as unix seconds, the vendor named,
    nothing internal."""
    return {k: v for k, v in d.items() if k != "id"} | {"vendor": vendors.name(d["manufacturer"])}


class Store:
    def __init__(self, path, readonly=False):
        """Open (and create) the database. readonly opens at the SQLite level, so
        the dashboard and the CLI cannot modify anything even by mistake."""
        path = Path(path)
        if readonly:
            # exists() is False on a permission error too, which would send a
            # user who is simply not the btlog user looking for a dead daemon.
            if (path.parent.is_dir() and not os.access(path.parent, os.R_OK | os.X_OK)) or \
                    (path.exists() and not os.access(path, os.R_OK)):
                raise PermissionError(f"cannot read {path} (run as the daemon's user, e.g. sudo -u btlog)")
            if not path.exists():
                raise FileNotFoundError(f"no database at {path} (is the daemon running?)")
            self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path))
            # WAL lets the CLI and the dashboard read while the daemon writes.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            if self.conn.execute("PRAGMA user_version").fetchone()[0] < VERSION:
                self.conn.execute(f"PRAGMA user_version = {VERSION}")
        # Read once: a reader lives for one command or one request, and the
        # tables it may query do not change under it.
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.has_readings = version >= 2
        self.conn.row_factory = _row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.create_function("fold", 1, _fold, deterministic=True)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- Writing ----------

    def record_cycle(self, ts, adverts):
        """Store one scan cycle started at ts: one sighting per address with the
        strongest RSSI, and the hourly rollups and cycle count advanced, so the
        trends are always current and never need recomputing. A sensor gets one
        reading per kind, the last value heard in the cycle, rolled up the same
        way. Returns the number of devices heard."""
        heard = {}
        for a in adverts:
            h = heard.get(a.address)
            if h is None:
                h = heard[a.address] = {"rssi": a.rssi, "n": 0, "name": None, "mfr": None, "readings": {},
                                        "kind": address_kind(a.address, a.address_type)}
            h["n"] += 1
            h["rssi"] = max(h["rssi"], a.rssi)
            h["name"] = printable(a.name) or h["name"]
            h["mfr"] = a.manufacturer if a.manufacturer is not None else h["mfr"]
            h["readings"].update(decoders.decode(a.manufacturer_data, a.service_data))
        day, hour, weekday = local_hour(ts)
        with self.conn:
            self.conn.execute(
                """INSERT INTO cycles VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(day, hour) DO UPDATE SET count = count + 1, last_ts = max(last_ts, excluded.last_ts)""",
                (day, hour, weekday, ts))
            for address, h in heard.items():
                # A kind learned later replaces 'unknown' (a backend that did not
                # say at first); a name or vendor, once known, is never unlearned.
                self.conn.execute(
                    """INSERT INTO devices (address, kind, name, manufacturer, first_seen, last_seen, cycles)
                       VALUES (?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(address) DO UPDATE SET
                           last_seen = excluded.last_seen, cycles = cycles + 1,
                           kind = CASE WHEN kind = 'unknown' THEN excluded.kind ELSE kind END,
                           name = COALESCE(excluded.name, name),
                           manufacturer = COALESCE(excluded.manufacturer, manufacturer)""",
                    (address, h["kind"], h["name"], h["mfr"], ts, ts))
                did = self.conn.execute("SELECT id FROM devices WHERE address = ?", (address,)).fetchone()["id"]
                self.conn.execute("INSERT OR REPLACE INTO sightings VALUES (?, ?, ?, ?)",
                                  (ts, did, h["rssi"], h["n"]))
                self.conn.execute(
                    """INSERT INTO hourly VALUES (?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(device_id, day, hour) DO UPDATE SET
                           cycles = cycles + 1, rssi_max = max(rssi_max, excluded.rssi_max),
                           rssi_sum = rssi_sum + excluded.rssi_sum""",
                    (day, hour, weekday, did, h["rssi"], h["rssi"]))
                for kind, value in h["readings"].items():
                    self.conn.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?)", (ts, did, kind, value))
                    self.conn.execute(
                        """INSERT INTO hourly_readings VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                           ON CONFLICT(device_id, kind, day, hour) DO UPDATE SET
                               n = n + 1, min = min(min, excluded.min), max = max(max, excluded.max),
                               sum = sum + excluded.sum""",
                        (did, day, hour, weekday, kind, value, value, value))
        return len(heard)

    def set_label(self, address, label):
        with self.conn:
            n = self.conn.execute("UPDATE devices SET label = ? WHERE address = ?",
                                  (label or None, address)).rowcount
        if not n:
            raise LookupError(f"no device {address}")

    def forget(self, address):
        """Remove one device with all its sightings and rollups (the cycle counts
        stay, the scanner did run). For the neighbour's thing you do not want in
        your database, without starting over."""
        with self.conn:
            n = self.conn.execute("DELETE FROM devices WHERE address = ? AND kind != ?",
                                  (address, FORGOTTEN)).rowcount
        if not n:
            raise LookupError(f"no device {address}")

    def purge(self, now, retention_days, forget_random_days):
        """Apply retention; returns (sightings deleted, random devices forgotten).
        Raw readings go with the sightings of the same age. 0 for either means
        keep forever: that is the "log everything" setting, so it must be an
        explicit no-op rather than "older than now". A forgotten address takes
        its readings along: folded together, strangers' readings mean nothing."""
        # -1 as the cutoff matches nothing: timestamps are never negative here.
        old = now - retention_days * 86400 if retention_days else -1
        unseen = now - forget_random_days * 86400 if forget_random_days else -1
        with self.conn:
            sightings = self.conn.execute("DELETE FROM sightings WHERE ts < ?", (old,)).rowcount
            self.conn.execute("DELETE FROM readings WHERE ts < ?", (old,))
            stale = ("kind IN (?, ?) AND last_seen < ?", (*PRIVATE_KINDS, unseen))
            if self.conn.execute(f"SELECT 1 FROM devices WHERE {stale[0]} LIMIT 1", stale[1]).fetchone():
                self.conn.execute(
                    """INSERT INTO devices (address, kind, first_seen, last_seen, cycles)
                       VALUES (?, ?, ?, ?, 0) ON CONFLICT(address) DO NOTHING""",
                    (FORGOTTEN, FORGOTTEN, now, now))
                sink = self.conn.execute("SELECT id FROM devices WHERE address = ?", (FORGOTTEN,)).fetchone()["id"]
                self.conn.execute(
                    f"""INSERT INTO hourly
                        SELECT h.day, h.hour, h.weekday, ?, h.cycles, h.rssi_max, h.rssi_sum
                        FROM hourly h JOIN devices d ON d.id = h.device_id WHERE {stale[0]}
                        ON CONFLICT(device_id, day, hour) DO UPDATE SET
                            cycles = cycles + excluded.cycles,
                            rssi_max = max(rssi_max, excluded.rssi_max),
                            rssi_sum = rssi_sum + excluded.rssi_sum""",
                    (sink, *stale[1]))
            devices = self.conn.execute(f"DELETE FROM devices WHERE {stale[0]}", stale[1]).rowcount
        return sightings, devices

    def backup(self, dest):
        """A consistent copy of the database through SQLite's backup API: a
        plain file copy of a WAL database can miss the last writes or tear."""
        with sqlite3.connect(str(dest)) as copy:
            self.conn.backup(copy)
        copy.close()

    # ---------- Reading ----------

    def _seen(self, since, until, private):
        """WHERE clause for devices seen in a period: rotating private addresses
        are left out unless asked for, as a phone alone leaves a hundred a day."""
        where, args = ["kind != ?"], [FORGOTTEN]
        if since is not None:
            where.append("last_seen >= ?")
            args.append(since)
        if until is not None:
            where.append("first_seen < ?")
            args.append(until)
        if not private:
            where.append("kind NOT IN (?, ?)")
            args += PRIVATE_KINDS
        return " AND ".join(where), args

    def devices(self, since=None, until=None, private=False):
        """Devices whose first-to-last span overlaps the period, by last
        sighting, newest first. That is the devices table alone, no sighting
        lookup, so a device absent for a day inside its span still lists."""
        where, args = self._seen(since, until, private)
        return self.conn.execute(
            f"""SELECT d.*, (SELECT COUNT(*) FROM hourly WHERE device_id = d.id) AS hours
                FROM devices d WHERE {where} ORDER BY last_seen DESC, address""", args).fetchall()

    def private_count(self, since=None, until=None):
        """How many rotating private addresses devices() left out."""
        where, args = self._seen(since, until, True)
        return self.conn.execute(
            f"SELECT COUNT(*) AS n FROM devices WHERE {where} AND kind IN (?, ?)", args + list(PRIVATE_KINDS)).fetchone()["n"]

    def find(self, text):
        """The one device whose address, label or name is text or starts with it,
        case insensitively. An exact address or label wins over prefix matches;
        anything else ambiguous, including two devices with the same label, is
        an error rather than a guess."""
        t = text.strip().casefold()
        like = t.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.conn.execute(
            """SELECT *, (fold(address) = ? OR fold(label) = ?) AS exact FROM devices WHERE kind != ? AND
               (fold(address) LIKE ? ESCAPE '\\' OR fold(label) LIKE ? ESCAPE '\\' OR fold(name) LIKE ? ESCAPE '\\')
               ORDER BY exact DESC, last_seen DESC""",
            (t, t, FORGOTTEN, like, like, like)).fetchall()
        if not rows:
            raise LookupError(f"no device matching {text!r}")
        if len(rows) > 1 and (not rows[0]["exact"] or rows[1]["exact"]):
            raise LookupError(f"{text!r} matches {len(rows)} devices; use the full address")
        return {k: v for k, v in rows[0].items() if k != "exact"}

    def sightings(self, since=None, until=None, device_id=None):
        """Raw sightings, oldest first, with the device's address, kind, name and label."""
        where, args = ["1"], []
        if since is not None:
            where.append("s.ts >= ?")
            args.append(since)
        if until is not None:
            where.append("s.ts < ?")
            args.append(until)
        if device_id is not None:
            where.append("s.device_id = ?")
            args.append(device_id)
        return self.conn.execute(
            f"""SELECT s.ts, d.address, d.kind, d.name, d.label, s.rssi, s.adverts
                FROM sightings s JOIN devices d ON d.id = s.device_id
                WHERE {' AND '.join(where)} ORDER BY s.ts, d.address""", args)

    def now(self):
        """The latest scan cycle: when it ran and the devices it heard, strongest
        first; and the latest reading of each kind from every sensor, with its
        time. That one is from any cycle, not just the last: a sensor may
        advertise less often than the scanner listens."""
        last = self.conn.execute("SELECT MAX(last_ts) AS ts FROM cycles").fetchone()["ts"]
        heard = self.conn.execute(
            """SELECT d.*, s.rssi, s.adverts FROM sightings s JOIN devices d ON d.id = s.device_id
               WHERE s.ts = ? ORDER BY s.rssi DESC, d.address""", (last,)).fetchall()
        latest = []
        if self.has_readings:
            # SQLite fills the bare column value from the row MAX(ts) picked.
            latest = self.conn.execute(
                """SELECT d.address, d.name, d.label, d.manufacturer, r.kind, r.value, r.ts
                   FROM (SELECT device_id, kind, value, MAX(ts) AS ts FROM readings GROUP BY device_id, kind) r
                   JOIN devices d ON d.id = r.device_id
                   ORDER BY fold(COALESCE(d.label, d.name, d.address)), d.address, r.kind""").fetchall()
        return {"last_cycle": last, "devices": [public(d) for d in heard],
                "readings": [public(r) | {"unit": decoders.UNITS.get(r["kind"], "")} for r in latest]}

    def status(self, sightings=True):
        """Counts and the last cycle, for `btlog status` and the dashboard header."""
        s = self.conn.execute(
            """SELECT (SELECT COUNT(*) FROM devices WHERE kind NOT IN ('rpa', 'nrpa', 'forgotten')) AS devices,
                      (SELECT COUNT(*) FROM devices WHERE kind IN ('rpa', 'nrpa')) AS private,
                      (SELECT COALESCE(SUM(count), 0) FROM cycles) AS cycles,
                      (SELECT MAX(last_ts) FROM cycles) AS last_cycle,
                      (SELECT MIN(day) FROM cycles) AS first_day""").fetchone()
        # Counting sightings walks the whole index, so the dashboard skips it.
        s["sightings"] = self.conn.execute("SELECT COUNT(*) AS n FROM sightings").fetchone()["n"] if sightings else None
        return s
