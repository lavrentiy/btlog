"""The rules behind `btlog alerts`, and the alerts table they keep.

Watching is opt-in per device: only a labelled device (`btlog name`) can raise
an alert, because a label is how you say "this one is mine". A stranger's
phone that stops coming by is not news, and btlog does not watch other
people.

Each rule is a function (store, conf, now) returning the alerts that hold
now, as {rule, device_id, key, message}. run() compares them with the open
episodes in the alerts table: a new one opens an episode, an open one that no
longer holds is closed, and each episode gets exactly one notification when
it opens and one when it closes. A notification that fails is tried again on
the next run, so the timer is the retry loop. An episode that closed before
its open notification ever got through is not notified at all: it is over,
and saying so twice, late, helps nobody; `btlog alerts` still lists it.

Absence cannot be told from a dead scanner, so while the scanner is stale (no
cycle for 3 intervals, as `btlog status --check` has it) silent, away and
range are not evaluated at all, their episodes stay as they are, and the
scanner alert opens instead.
"""

import time

from btlog import notify, trends, when
from btlog.decoders import UNITS
from btlog.store import local_hour, public

RULES = ("silent", "away", "range", "unusual")   # what config `alerts` may name; scanner is always on
UNUSUAL_SHARE, UNUSUAL_DAYS, KNOWN_DAYS = 0.02, 30, 7

# What the close notification says; the open one says the episode's message.
CLOSED = {"silent": "{name} heard again", "away": "{name} is back", "range": "{name} {key} back in range",
          "unusual": "{name}: the unusual hour is over", "scanner": "scanner running again"}


def stale(last_cycle, interval, now):
    """True when the scanner has not completed a cycle for 3 intervals."""
    return last_cycle is None or now - last_cycle > 3 * interval


def _alert(rule, device, message, key=""):
    return {"rule": rule, "device_id": device["id"] if device else None, "key": key, "message": message}


def _labelled(store, readings):
    """Labelled devices that have (or have never had) a sensor reading."""
    return store.conn.execute(
        f"""SELECT * FROM devices d WHERE label IS NOT NULL AND
            {'' if readings else 'NOT'} EXISTS (SELECT 1 FROM hourly_readings WHERE device_id = d.id)""").fetchall()


def silent(store, conf, now):
    """A labelled sensor not heard for silent_hours: flat battery, out of range, broken."""
    return [_alert("silent", d, f"{d['label']} not heard since {when.fmt(d['last_seen'])}")
            for d in _labelled(store, True) if now - d["last_seen"] > conf.silent_hours * 3600]


def away(store, conf, now):
    """A labelled device that is not a sensor, not heard for away_minutes. The
    minutes are the debounce: a watch at the edge of range drops out of a
    cycle or two without having left."""
    return [_alert("away", d, f"{d['label']} left")
            for d in _labelled(store, False) if now - d["last_seen"] > conf.away_minutes * 60]


def in_range(store, conf, now):
    """The latest reading of each configured kind outside its bounds. Only a
    reading from the last silent_hours counts: an older one says nothing about
    now, and silent covers a sensor that stopped talking."""
    out = []
    for label, kinds in conf.range.items():
        for d in store.conn.execute("SELECT * FROM devices WHERE fold(label) = ?", (label.casefold(),)).fetchall():
            for kind, (low, high) in kinds.items():
                # SQLite fills the bare column value from the row MAX(ts) picked.
                r = store.conn.execute(
                    "SELECT value, MAX(ts) AS ts FROM readings WHERE device_id = ? AND kind = ? AND ts >= ?",
                    (d["id"], kind, now - conf.silent_hours * 3600)).fetchone()
                if r["ts"] is not None and not low <= r["value"] <= high:
                    out.append(_alert("range", d, f"{d['label']} {kind} {r['value']:g} {UNITS[kind]} outside "
                                                  f"{low:g} to {high:g}", key=kind))
    return out


def unusual(store, conf, now):
    """A labelled device heard in this hour of day, when over the last 30 days
    it was around in that hour less than 2 % of the time: the watch at home at
    three in the afternoon on a workday. A device known for less than a week
    has no habits yet. The episode lasts as long as the device keeps being
    heard in hours that are unusual for it."""
    day, hour, _ = local_hour(now)
    out = []
    for d in store.conn.execute(
            """SELECT d.* FROM devices d JOIN hourly h ON h.device_id = d.id AND h.day = ? AND h.hour = ?
               WHERE d.label IS NOT NULL AND d.first_seen <= ?""", (day, hour, now - KNOWN_DAYS * 86400)).fetchall():
        share = trends.hours(store, d["id"], since=now - UNUSUAL_DAYS * 86400, until=now)[hour]
        if share is not None and share < UNUSUAL_SHARE:
            out.append(_alert("unusual", d, f"{d['label']} here at {hour:02d}:00, around then "
                                            f"{share:.0%} of the last {UNUSUAL_DAYS} days"))
    return out


EVALUATE = {"silent": silent, "away": away, "range": in_range, "unusual": unusual}


def run(store, conf, now=None):
    """Evaluate the enabled rules once, update the alerts table, deliver what
    is pending. The table is written in two short transactions, never across
    a network call, so the daemon is not kept waiting for SQLite's lock.
    Returns counts and the one-line errors, for the CLI to print."""
    now = time.time() if now is None else now
    last = store.status(sightings=False)["last_cycle"]
    holding = [_alert("scanner", None, f"scanner has not run since {when.fmt(last)}" if last
                      else "scanner has not run yet")] if stale(last, conf.interval, now) else []
    # Unusual needs no guard: it fires on a device heard, which a dead scanner cannot fake.
    evaluated = ["scanner", *(r for r in conf.alerts if r == "unusual" or not holding)]
    for rule in evaluated[1:]:
        holding += EVALUATE[rule](store, conf, now)
    opened, closed = _sync(store, now, evaluated, holding, ["scanner", *conf.alerts])
    # A label is checked here, not when the config is loaded: only the
    # database knows them, and a misspelt one would quietly watch nothing.
    errors = [f"range: no device labelled {label!r}" for label in conf.range if "range" in conf.alerts and not
              store.conn.execute("SELECT 1 FROM devices WHERE fold(label) = ?", (label.casefold(),)).fetchone()]
    sent = _deliver(store, conf.notify, now, errors) if conf.notify else 0
    still = store.conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE closed IS NULL").fetchone()["n"]
    return {"opened": opened, "closed": closed, "open": still, "sent": sent, "errors": errors}


def _sync(store, now, evaluated, holding, enabled):
    """Open what newly holds, close what stopped holding among the rules that
    were evaluated. Rules skipped this run (the scanner guard) keep their
    episodes as they are; a rule taken out of the config loses its open ones,
    which would otherwise stay open for good."""
    with store.conn:
        store.conn.execute(f"DELETE FROM alerts WHERE closed IS NULL AND rule NOT IN ({','.join('?' * len(enabled))})",
                           enabled)
        episodes = {(a["rule"], a["device_id"], a["key"]): a["id"] for a in store.conn.execute(
            "SELECT id, rule, device_id, key FROM alerts WHERE closed IS NULL")}
        now_holding = {(a["rule"], a["device_id"], a["key"]): a for a in holding}
        for k, a in now_holding.items():
            if k not in episodes:
                store.conn.execute("INSERT INTO alerts (rule, device_id, key, opened, message) VALUES (?, ?, ?, ?, ?)",
                                   (*k, now, a["message"]))
        ended = [i for k, i in episodes.items() if k[0] in evaluated and k not in now_holding]
        store.conn.executemany("UPDATE alerts SET closed = ? WHERE id = ?", [(now, i) for i in ended])
    return len(now_holding.keys() - episodes.keys()), len(ended)


def _deliver(store, target, now, errors):
    """Send the pending notifications, oldest first; the first failure ends
    the run's sending, so a dead target costs one timeout, not one per alert,
    and the order is kept for the next run. Each success is recorded at once."""
    pending = store.conn.execute(
        """SELECT a.*, d.address, COALESCE(d.label, d.name, d.address) AS name
           FROM alerts a LEFT JOIN devices d ON d.id = a.device_id
           WHERE (a.closed IS NULL AND a.sent_open IS NULL)
              OR (a.closed IS NOT NULL AND a.sent_open IS NOT NULL AND a.sent_closed IS NULL)
           ORDER BY COALESCE(a.closed, a.opened), a.id""").fetchall()
    sent = 0
    for a in pending:
        state = "closed" if a["closed"] is not None else "open"
        message = CLOSED[a["rule"]].format(name=a["name"], key=a["key"]) if state == "closed" else a["message"]
        alert = {"rule": a["rule"], "state": state, "device": a["address"], "name": a["name"],
                 "message": message, "ts": a["closed"] if state == "closed" else a["opened"]}
        try:
            notify.send(target, alert)
        except OSError as e:
            errors.append(f"notify: {e}; {len(pending) - sent} pending, next run tries again")
            break
        with store.conn:
            store.conn.execute(f"UPDATE alerts SET sent_{state} = ? WHERE id = ?", (now, a["id"]))
        sent += 1
    return sent


def listing(store, closed=20):
    """{"open": [...], "closed": [the last `closed` ones]}, newest first, for
    `btlog alerts` and /api/alerts. Empty on a file from before alerts."""
    if not store.has_alerts:
        return {"open": [], "closed": []}
    q = """SELECT a.rule, d.address AS device, COALESCE(d.label, d.name) AS name, a.key, a.message,
                  a.opened, a.closed, a.sent_open, a.sent_closed
           FROM alerts a LEFT JOIN devices d ON d.id = a.device_id"""
    return {"open": store.conn.execute(f"{q} WHERE a.closed IS NULL ORDER BY a.opened DESC, a.id DESC").fetchall(),
            "closed": store.conn.execute(f"{q} WHERE a.closed IS NOT NULL ORDER BY a.closed DESC, a.id DESC LIMIT ?",
                                         (closed,)).fetchall()}


def present(store, away_minutes, now=None):
    """Labelled devices heard within away_minutes, the same test away uses:
    who is home, for Home Assistant or a shell script."""
    now = time.time() if now is None else now
    return [public(d) for d in store.conn.execute(
        "SELECT * FROM devices WHERE label IS NOT NULL AND last_seen >= ? ORDER BY fold(label), address",
        (now - away_minutes * 60,))]
