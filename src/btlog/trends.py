"""The trend queries behind `btlog hours`, `days`, `heat`, `rssi`, `new`, `top`
and the dashboard. All of them read the hourly rollups, never the raw
sightings, so they give the same answer before and after the raw rows are
purged, and all of them cut at the hour `since` falls in.

Presence is always measured against the cycles the scanner actually ran in the
same hours. For one device that is the fraction of cycles that heard it: 0.9 at
08 means it is nearly always around at eight. For all devices it is the average
number of devices heard per cycle. None marks a slot the scanner never covered.
"""

from btlog.store import after

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def hours(store, device_id=None, since=None):
    """Presence per hour of day, index 0..23."""
    return _by(store, "hour", 24, device_id, since)


def weekdays(store, device_id=None, since=None):
    """Presence per weekday, index 0 (Monday) .. 6 (Sunday)."""
    return _by(store, "weekday", 7, device_id, since)


def _by(store, col, n, device_id, since):
    cond, args = after(since)
    ran = {r[col]: r["n"] for r in store.conn.execute(
        f"SELECT {col}, SUM(count) AS n FROM cycles WHERE {cond} GROUP BY {col}", args)}
    dev = "AND device_id = ?" if device_id is not None else ""
    heard = {r[col]: r["n"] for r in store.conn.execute(
        f"SELECT {col}, SUM(cycles) AS n FROM hourly WHERE {cond} {dev} GROUP BY {col}",
        args + ((device_id,) if device_id is not None else ()))}
    return [heard.get(i, 0) / ran[i] if ran.get(i) else None for i in range(n)]


def timeline(store, device_id=None, since=None):
    """Presence per hour over time, oldest first: [{"day", "hour", "presence"}],
    the same measure as hours() but not folded onto one day: the share of that
    hour's cycles that heard the device, or without a device the average number
    of devices heard. Hours the scanner ran but nothing was heard are included
    with 0 and hours it did not run are missing, so gaps show as gaps."""
    cond, args = after(since, "c")
    dev = "AND h.device_id = ?" if device_id is not None else ""
    return store.conn.execute(
        f"""SELECT c.day, c.hour, COALESCE(SUM(h.cycles), 0) * 1.0 / c.count AS presence
            FROM cycles c LEFT JOIN hourly h ON h.day = c.day AND h.hour = c.hour {dev}
            WHERE {cond} GROUP BY c.day, c.hour ORDER BY c.day, c.hour""",
        ((device_id,) if device_id is not None else ()) + args).fetchall()


def rssi(store, device_id, since=None, by="day"):
    """Signal strength over time: [{"when", "max", "avg"}] per local day or hour.
    RSSI is in dBm, so higher (closer to 0) is nearer; a falling trend means the
    device moved away or something got in between."""
    if by not in ("day", "hour"):
        raise ValueError("by must be 'day' or 'hour'")
    cond, args = after(since)
    if by == "hour":
        return store.conn.execute(
            f"""SELECT day || ' ' || printf('%02d', hour) AS "when", rssi_max AS max,
                       rssi_sum * 1.0 / cycles AS avg
                FROM hourly WHERE device_id = ? AND {cond} ORDER BY day, hour""",
            (device_id, *args)).fetchall()
    return store.conn.execute(
        f"""SELECT day AS "when", MAX(rssi_max) AS max, SUM(rssi_sum) * 1.0 / SUM(cycles) AS avg
            FROM hourly WHERE device_id = ? AND {cond} GROUP BY day ORDER BY day""",
        (device_id, *args)).fetchall()


def new(store, since):
    """Devices first seen at or after since, newest first. Rotating private
    addresses are left out: every one of them is new, which tells you nothing."""
    return store.conn.execute(
        """SELECT * FROM devices WHERE first_seen >= ? AND kind NOT IN ('rpa', 'nrpa', 'forgotten')
           ORDER BY first_seen DESC""", (since,)).fetchall()


def busiest(store, since=None, limit=10):
    """Devices by presence since a time, most present first: who is here the most.
    Rotating private addresses are left out, as each lives a quarter of an hour."""
    cond, args = after(since, "h")
    return store.conn.execute(
        f"""SELECT d.*, COUNT(*) AS hours, SUM(h.cycles) AS cycles_heard
            FROM hourly h JOIN devices d ON d.id = h.device_id
            WHERE {cond} AND d.kind NOT IN ('rpa', 'nrpa', 'forgotten')
            GROUP BY d.id ORDER BY cycles_heard DESC, d.last_seen DESC LIMIT ?""",
        (*args, limit)).fetchall()
