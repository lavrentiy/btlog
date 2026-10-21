"""The trend queries behind `btlog hours`, `days`, `rssi`, `new`, `show` and the
dashboard. All of them read the hourly rollups, never the raw sightings, so they
give the same answer before and after the raw rows are purged.

Presence is always measured against the cycles the scanner actually ran in the
same hours. For one device that is the fraction of cycles that heard it: 0.9 at
08 means it is nearly always around at eight. For all devices it is the average
number of devices heard per cycle. None marks a slot the scanner never covered.
"""

from btlog import when

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def hours(store, device_id=None, since=None):
    """Presence per hour of day, index 0..23."""
    return _by(store, "hour", 24, device_id, since)


def weekdays(store, device_id=None, since=None):
    """Presence per weekday, index 0 (Monday) .. 6 (Sunday)."""
    return _by(store, "weekday", 7, device_id, since)


def _by(store, col, n, device_id, since):
    day0 = when.day(since)
    ran = {r[col]: r["n"] for r in store.conn.execute(
        f"SELECT {col}, SUM(count) AS n FROM cycles WHERE day >= ? GROUP BY {col}", (day0,))}
    dev = "AND device_id = ?" if device_id is not None else ""
    args = (day0, device_id) if device_id is not None else (day0,)
    heard = {r[col]: r["n"] for r in store.conn.execute(
        f"SELECT {col}, SUM(cycles) AS n FROM hourly WHERE day >= ? {dev} GROUP BY {col}", args)}
    return [heard.get(i, 0) / ran[i] if ran.get(i) else None for i in range(n)]


def timeline(store, device_id, since=None):
    """Presence per hour over time, oldest first: [{"day", "hour", "presence"}] with
    the fraction of that hour's cycles that heard the device. Hours the scanner
    ran but the device was absent are included with 0, so gaps show as gaps."""
    rows = store.conn.execute(
        """SELECT c.day, c.hour, COALESCE(h.cycles, 0) * 1.0 / c.count AS presence
           FROM cycles c LEFT JOIN hourly h ON h.day = c.day AND h.hour = c.hour AND h.device_id = ?
           WHERE c.day >= ? ORDER BY c.day, c.hour""", (device_id, when.day(since))).fetchall()
    return rows


def rssi(store, device_id, since=None, by="day"):
    """Signal strength over time: [{"when", "max", "avg"}] per local day or hour.
    RSSI is in dBm, so higher (closer to 0) is nearer; a falling trend means the
    device moved away or something got in between."""
    if by == "hour":
        return store.conn.execute(
            """SELECT day || ' ' || printf('%02d', hour) AS "when", rssi_max AS max,
                      rssi_sum * 1.0 / cycles AS avg
               FROM hourly WHERE device_id = ? AND day >= ? ORDER BY day, hour""",
            (device_id, when.day(since))).fetchall()
    if by != "day":
        raise ValueError("by must be 'day' or 'hour'")
    return store.conn.execute(
        """SELECT day AS "when", MAX(rssi_max) AS max, SUM(rssi_sum) * 1.0 / SUM(cycles) AS avg
           FROM hourly WHERE device_id = ? AND day >= ? GROUP BY day ORDER BY day""",
        (device_id, when.day(since))).fetchall()


def new(store, since):
    """Devices first seen at or after since, newest first. Rotating private
    addresses are left out: every one of them is new, which tells you nothing."""
    return store.conn.execute(
        """SELECT * FROM devices WHERE first_seen >= ? AND kind NOT IN ('rpa', 'nrpa', 'forgotten')
           ORDER BY first_seen DESC""", (since,)).fetchall()


def busiest(store, since=None, limit=10):
    """Devices by presence since a day, most present first: who is here the most.
    Rotating private addresses are left out, as each lives a quarter of an hour."""
    return store.conn.execute(
        """SELECT d.*, COUNT(*) AS hours, SUM(h.cycles) AS cycles_heard
           FROM hourly h JOIN devices d ON d.id = h.device_id
           WHERE h.day >= ? AND d.kind NOT IN ('rpa', 'nrpa', 'forgotten')
           GROUP BY d.id ORDER BY cycles_heard DESC, d.last_seen DESC LIMIT ?""",
        (when.day(since), limit)).fetchall()
