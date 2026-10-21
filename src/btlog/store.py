"""SQLite storage: every sighting, plus hourly rollups that outlive the raw rows.

  devices    one row per address: kind, best name, optional label, first/last seen.
  sightings  one row per device per scan cycle, with the strongest RSSI heard in
             that cycle. Deleted after retention_days; this is the bulk of the file.
  hourly     per device per local hour: cycles heard, best and summed RSSI. Tiny
             and kept forever, so hour-of-day and weekday trends work over years.
  cycles     per local hour: how many scan cycles the daemon ran. Presence is a
             fraction of that rather than of the wall clock, so a day the scanner
             was off does not read as a day nobody was home.

Hours are local time, because "who is here at eight in the morning" is a local
question; day and weekday are stored alongside so GROUP BY stays in SQL.

Private random addresses (resolvable and non-resolvable) rotate every few
minutes by design. One that has not been seen for forget_random_days is a dead
end for trends and only a privacy liability, so purge() removes it; its hourly
rows are folded into a single "forgotten" pseudo-device first, so the count of
devices around per hour stays right after the address is gone.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

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
    adverts INTEGER NOT NULL,       -- advertisements heard in the cycle
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
CREATE TABLE IF NOT EXISTS cycles (
    day TEXT NOT NULL,
    hour INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    count INTEGER NOT NULL,
    last_ts REAL NOT NULL,          -- start of the latest cycle in this hour
    PRIMARY KEY (day, hour)
) WITHOUT ROWID;
"""

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


def after(since, table=""):
    """SQL condition and parameters for rollup rows at or after since, to the
    hour. The rollups are keyed on (day, hour), so '--since 2h' must not pull
    in the whole of today. None (for 'all') matches everything."""
    if since is None:
        return "1", ()
    day, hour, _ = local_hour(since)
    t = f"{table}." if table else ""
    return f"({t}day > ? OR ({t}day = ? AND {t}hour >= ?))", (day, day, hour)


def printable(text):
    """A device name with control characters removed. Names come from strangers'
    advertisements and go to terminals, so an escape sequence that clears the
    screen or retitles the window must not get through."""
    return "".join(c for c in text if c.isprintable()) or None if text else None


def _row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Store:
    def __init__(self, path, readonly=False):
        """Open (and create) the database. readonly opens at the SQLite level, so
        the dashboard and the CLI cannot modify anything even by mistake."""
        path = Path(path)
        if readonly:
            if not path.exists():
                raise FileNotFoundError(f"no database at {path} (is the daemon running?)")
            self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path))
            # WAL lets the CLI and the dashboard read while the daemon writes.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = _row
        self.conn.execute("PRAGMA foreign_keys=ON")

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
        trends are always current and never need recomputing. Returns the number
        of devices heard."""
        heard = {}
        for a in adverts:
            h = heard.get(a.address)
            if h is None:
                h = heard[a.address] = {"rssi": a.rssi, "n": 0, "name": None, "mfr": None,
                                        "kind": address_kind(a.address, a.address_type)}
            h["n"] += 1
            h["rssi"] = max(h["rssi"], a.rssi)
            h["name"] = printable(a.name) or h["name"]
            h["mfr"] = a.manufacturer if a.manufacturer is not None else h["mfr"]
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
        """Apply retention; returns (sightings deleted, random devices forgotten)."""
        old = now - retention_days * 86400
        unseen = now - forget_random_days * 86400
        with self.conn:
            sightings = self.conn.execute("DELETE FROM sightings WHERE ts < ?", (old,)).rowcount
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
                self.conn.execute(
                    f"""UPDATE devices SET cycles = cycles + (SELECT COALESCE(SUM(cycles), 0) FROM devices WHERE {stale[0]}),
                        first_seen = min(first_seen, (SELECT min(first_seen) FROM devices WHERE {stale[0]}))
                        WHERE id = ?""", (*stale[1], *stale[1], sink))
            devices = self.conn.execute(f"DELETE FROM devices WHERE {stale[0]}", stale[1]).rowcount
        return sightings, devices

    # ---------- Reading ----------

    def devices(self, since=None, private=False):
        """Devices by last sighting, newest first. since limits to those seen after
        it. Rotating private addresses are left out unless private is set: a phone
        alone leaves a hundred of them a day, which would bury the real list."""
        where, args = ["kind != ?"], [FORGOTTEN]
        if since is not None:
            where.append("last_seen >= ?")
            args.append(since)
        if not private:
            where.append("kind NOT IN (?, ?)")
            args += PRIVATE_KINDS
        return self.conn.execute(
            f"""SELECT d.*, (SELECT COUNT(*) FROM hourly WHERE device_id = d.id) AS hours
                FROM devices d WHERE {' AND '.join(where)} ORDER BY last_seen DESC, address""", args).fetchall()

    def forgotten(self):
        """The forgotten-random-addresses pseudo-device, or None."""
        return self.conn.execute("SELECT * FROM devices WHERE address = ?", (FORGOTTEN,)).fetchone()

    def find(self, text):
        """The one device whose address, label or name is text or starts with it,
        case insensitively. An exact address or label wins over prefix matches;
        anything else ambiguous is an error rather than a guess."""
        t = text.strip().upper()
        like = t.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.conn.execute(
            """SELECT *, (address = ? OR upper(label) = ?) AS exact FROM devices WHERE kind != ? AND
               (address LIKE ? ESCAPE '\\' OR upper(label) LIKE ? ESCAPE '\\' OR upper(name) LIKE ? ESCAPE '\\')
               ORDER BY exact DESC, last_seen DESC""",
            (t, t, FORGOTTEN, like, like, like)).fetchall()
        if not rows:
            raise LookupError(f"no device matching {text!r}")
        if len(rows) > 1 and not rows[0]["exact"]:
            raise LookupError(f"{text!r} matches {len(rows)} devices; use the full address")
        return {k: v for k, v in rows[0].items() if k != "exact"}

    def sightings(self, since=None, device_id=None):
        """Raw sightings, oldest first, with the device's address, kind, name and label."""
        where, args = ["1"], []
        if since is not None:
            where.append("s.ts >= ?")
            args.append(since)
        if device_id is not None:
            where.append("s.device_id = ?")
            args.append(device_id)
        return self.conn.execute(
            f"""SELECT s.ts, d.address, d.kind, d.name, d.label, s.rssi, s.adverts
                FROM sightings s JOIN devices d ON d.id = s.device_id
                WHERE {' AND '.join(where)} ORDER BY s.ts, d.address""", args)

    def status(self):
        """Counts and the last cycle, for `btlog status` and the dashboard header."""
        one = lambda sql: self.conn.execute(sql).fetchone()  # noqa: E731
        return {
            "devices": one("SELECT COUNT(*) AS n FROM devices WHERE kind NOT IN ('rpa', 'nrpa', 'forgotten')")["n"],
            "private": one("SELECT COUNT(*) AS n FROM devices WHERE kind IN ('rpa', 'nrpa')")["n"],
            "sightings": one("SELECT COUNT(*) AS n FROM sightings")["n"],
            "cycles": one("SELECT COALESCE(SUM(count), 0) AS n FROM cycles")["n"],
            "last_cycle": one("SELECT MAX(last_ts) AS t FROM cycles")["t"],
            "first_day": one("SELECT MIN(day) AS d FROM cycles")["d"],
        }
