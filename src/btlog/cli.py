"""btlog: a bluesitting daemon. It sits at home all day and night, logs the
Bluetooth LE devices in proximity, and answers questions about the trends.

  btlog devices [--since 7d]       devices heard, newest sighting first (--private adds rotating ones)
  btlog show DEVICE                one device: when it is around, by hour and weekday
  btlog hours [DEVICE]             presence per hour of day (all devices: how many)
  btlog days [DEVICE]              presence per weekday
  btlog heat [DEVICE] [--since 14d] presence calendar: one row per day, one cell per hour
  btlog rssi DEVICE [--by hour]    signal strength over time
  btlog new [--since 1d]           devices seen for the first time recently
  btlog top [--since 7d]           devices around the most
  btlog name DEVICE LABEL          label a device ("" removes the label)
  btlog forget DEVICE              remove one device and everything recorded about it
  btlog export [--since 1d]        raw sightings as CSV (or JSON with --json)
  btlog purge                      apply retention now (the daemon does it hourly)
  btlog status [--check]           database size and the last scan cycle (--check: exit 1 if stale)
  btlog web                        read-only dashboard on http://127.0.0.1:8778 (no authentication)
  btlog daemon                     the scanner itself (runs as a service)

DEVICE is an address, a label, a name or an unambiguous prefix of one.
--since takes 30m, 2h, 3d, today, yesterday, 2026-10-18, 2026-10-18T19:30 or all.
"""

import argparse
import csv
import json
import logging
import os
import signal
import sqlite3
import sys
import time

from btlog import __version__, config as cfg, trends, vendors, when
from btlog.store import Store

BAR = " ▁▂▃▄▅▆▇█"
HOURS = [f"{h:02d}" for h in range(24)]


# ---------- Output helpers ----------

def bar(value, scale, width=20):
    """A horizontal bar of width cells for value out of scale, in eighths."""
    if value is None or not scale:
        return "·" * width if value is None else " " * width
    eighths = round(min(value, scale) / scale * width * 8)
    full, part = "█" * (eighths // 8), BAR[eighths % 8].strip()
    return full + part + " " * (width - len(full) - len(part))


def table(rows, head):
    """Columns padded to the widest cell, header first."""
    rows = [head] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def emit(a, data, text):
    print(json.dumps(data, indent=2) if a.json else text)


def who(d):
    """Label, else name, else the vendor: how a device is called in tables."""
    return d["label"] or d["name"] or (f"{vendors.name(d['manufacturer'])} device" if d["manufacturer"] is not None else "")


def device_row(d):
    return [d["address"], d["kind"], who(d), vendors.name(d["manufacturer"]),
            when.fmt(d["first_seen"]), when.fmt(d["last_seen"]), d["hours"]]


def public(d):
    """A device as JSON: times as unix seconds, the vendor named, nothing internal."""
    return {k: v for k, v in d.items() if k != "id"} | {"vendor": vendors.name(d["manufacturer"])}


def presence_lines(values, labels, scale, fmt):
    """One line per slot. Fractions of a device's presence are always drawn
    against 100%, so a device that is around 20% of the time gets a short bar
    rather than a full one; device counts are drawn against their maximum."""
    return "\n".join(f"{lab:>3}  {bar(v, scale)}  {'' if v is None else fmt.format(v)}".rstrip()
                     for lab, v in zip(labels, values))


def presence(values, labels, device):
    if device:
        return presence_lines(values, labels, 1.0, "{:.0%}")
    return presence_lines(values, labels, max((v for v in values if v is not None), default=0), "{:.1f}")


# ---------- Commands ----------

def cmd_devices(store, a):
    since = when.parse(a.since)
    rows = store.devices(since, private=a.private)
    text = table([device_row(d) for d in rows], ["ADDRESS", "KIND", "LABEL/NAME", "VENDOR", "FIRST SEEN", "LAST SEEN", "HOURS"])
    if not a.private:
        n = len(store.devices(since, private=True)) - len(rows)
        text += f"\n({n} rotating private addresses not shown; --private lists them)"
    emit(a, [public(d) for d in rows], text)


def cmd_show(store, a):
    d = store.find(a.device)
    since = when.parse(a.since)
    hours, days = trends.hours(store, d["id"], since), trends.weekdays(store, d["id"], since)
    emit(a, {**public(d), "hours": hours, "weekdays": days}, "\n".join([
        f"{d['address']}  {d['kind']}  {who(d)}".rstrip(),
        f"first seen {when.fmt(d['first_seen'])}, last seen {when.fmt(d['last_seen'])}, "
        f"{d['cycles']} sightings, vendor {vendors.name(d['manufacturer']) or '-'}",
        "", f"presence by hour of day since {a.since} (share of scan cycles):", presence(hours, HOURS, d),
        "", "by weekday:", presence(days, trends.WEEKDAYS, d)]))


def _presence(store, a, fn, labels, title):
    since = when.parse(a.since)
    d = store.find(a.device) if a.device else None
    values = fn(store, d["id"] if d else None, since)
    text = (f"{title} for {d['address']} {who(d)}".rstrip() + f" since {a.since} (share of scan cycles)" if d
            else f"{title} since {a.since} (average devices heard per scan cycle)")
    emit(a, values, text + "\n" + presence(values, labels, d))


def cmd_hours(store, a):
    _presence(store, a, trends.hours, HOURS, "presence by hour of day")


def cmd_days(store, a):
    _presence(store, a, trends.weekdays, trends.WEEKDAYS, "presence by weekday")


def cmd_heat(store, a):
    """One row per day, one cell per hour: a presence calendar."""
    since = when.parse(a.since)
    d = store.find(a.device) if a.device else None
    rows = trends.timeline(store, d["id"] if d else None, since)
    days = {}
    for r in rows:
        days.setdefault(r["day"], [None] * 24)[r["hour"]] = r["presence"]
    scale = 1.0 if d else max((r["presence"] for r in rows), default=0)
    head = "            " + "".join(f"{h:<3d}" for h in range(0, 24, 3))
    text = (f"presence of {d['address']} {who(d)}".rstrip() if d else "devices heard per scan cycle") \
        + f" since {a.since}, one row per day, one cell per hour" + ("" if d else f" (full cell = {scale:.1f})") + "\n"
    text += head + "\n" + "\n".join(
        day + "  " + "".join("·" if v is None else BAR[round(min(v, scale) / scale * 8)] if scale else " " for v in cells)
        for day, cells in days.items())
    emit(a, [dict(r) for r in rows], text)


def cmd_rssi(store, a):
    d = store.find(a.device)
    rows = trends.rssi(store, d["id"], when.parse(a.since), a.by)
    # RSSI is negative dBm; bars grow from -100 towards 0 so nearer reads as longer.
    text = f"signal of {d['address']} {who(d)} by {a.by} since {a.since} (dBm, max / average)\n"
    text += "\n".join(f"{r['when']}  {bar(max(r['avg'] + 100, 0), 100)}  {r['max']:4d} / {r['avg']:6.1f}" for r in rows)
    emit(a, rows, text)


def cmd_new(store, a):
    since = when.parse(a.since)
    if since is None:
        raise ValueError("new needs a --since other than all")
    rows = trends.new(store, since)
    emit(a, [public(d) for d in rows],
         table([[d["address"], d["kind"], who(d), when.fmt(d["first_seen"]), d["cycles"]] for d in rows],
               ["ADDRESS", "KIND", "LABEL/NAME", "FIRST SEEN", "SIGHTINGS"]))


def cmd_top(store, a):
    rows = trends.busiest(store, when.parse(a.since), a.limit)
    emit(a, [public(d) for d in rows],
         table([[d["address"], d["kind"], who(d), d["hours"], d["cycles_heard"]] for d in rows],
               ["ADDRESS", "KIND", "LABEL/NAME", "HOURS", "SIGHTINGS"]))


def cmd_name(store, a):
    d = store.find(a.device)
    store.set_label(d["address"], a.label)
    print(f"{d['address']} is now {a.label!r}" if a.label else f"{d['address']} label removed")


def cmd_forget(store, a):
    d = store.find(a.device)
    store.forget(d["address"])
    print(f"{d['address']} {who(d)} forgotten, with {d['cycles']} sightings".replace("  ", " "))


def cmd_export(store, a):
    """Sightings as CSV: unix seconds and local time, so the hour that repeats
    when daylight saving ends is still unambiguous."""
    since = when.parse(a.since)
    did = store.find(a.device)["id"] if a.device else None
    rows = store.sightings(since, did)
    if a.json:
        return print(json.dumps([dict(s) for s in rows], indent=2))
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["ts", "time", "address", "kind", "name", "label", "rssi", "adverts"])
    for s in rows:
        out.writerow([int(s["ts"]), when.fmt(s["ts"], seconds=True), s["address"], s["kind"], s["name"] or "",
                      s["label"] or "", s["rssi"], s["adverts"]])


def cmd_purge(store, a, conf):
    sightings, devices = store.purge(time.time(), conf.retention_days, conf.forget_random_days)
    print(f"deleted {sightings} sightings older than {conf.retention_days} days, "
          f"forgot {devices} private addresses unseen for {conf.forget_random_days} days")


def cmd_status(store, a, conf):
    s = store.status()
    stale = s["last_cycle"] is None or time.time() - s["last_cycle"] > 3 * conf.interval
    emit(a, {**s, "stale": stale}, "\n".join([
        f"database   {conf.db}",
        f"devices    {s['devices']} (plus {s['private']} rotating private addresses)",
        f"sightings  {s['sightings']} (kept {conf.retention_days} days)",
        f"cycles     {s['cycles']} since {s['first_day'] or '-'}",
        f"last cycle {when.fmt(s['last_cycle'], seconds=True)}" + (" (stale: is the daemon running?)" if stale else "")]))
    return 1 if a.check and stale else 0


def cmd_web(a, conf):
    from btlog.web import serve
    serve(conf.db, a.bind or conf.web_bind)


def cmd_daemon(conf):
    from btlog.scanner import BleakSource, run
    logging.basicConfig(level=logging.DEBUG if os.environ.get("BTLOG_DEBUG") else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    # SystemExit unwinds through the event loop, which stops discovery cleanly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with Store(conf.db) as store:
        logging.getLogger("btlog").info("scanning every %gs into %s", conf.interval, conf.db)
        run(BleakSource(conf.adapter), store, conf)


# ---------- Main ----------

def parser():
    # The global flags are accepted before and after the subcommand. On the
    # subparser copies they default to SUPPRESS, so an absent flag there does
    # not overwrite what the main parser already set.
    def common(default):
        c = argparse.ArgumentParser(add_help=False)
        c.add_argument("--config", default=default, help="TOML config (default $BTLOG_CONFIG or /etc/btlog.toml)")
        c.add_argument("--db", default=default, help="database file, overriding the config")
        c.add_argument("--json", action="store_true", default=default, help="JSON output for scripts")
        return c

    p = argparse.ArgumentParser(prog="btlog", description=__doc__, parents=[common(None)],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.set_defaults(json=False)
    p.add_argument("--version", action="version", version=f"btlog {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, since=None, device=False):
        s = sub.add_parser(name, help=name, parents=[common(argparse.SUPPRESS)])
        if since:
            s.add_argument("--since", default=since, help=f"30m, 2h, 3d, today, 2026-10-18 or all (default {since})")
        if device == "optional":
            s.add_argument("device", nargs="?")
        elif device:
            s.add_argument("device")
        return s

    add("devices", since="all").add_argument("--private", action="store_true", help="include rotating private addresses")
    add("show", since="30d", device=True)
    add("hours", since="30d", device="optional")
    add("days", since="90d", device="optional")
    add("heat", since="14d", device="optional")
    add("rssi", since="7d", device=True).add_argument("--by", choices=["day", "hour"], default="day")
    add("new", since="1d")
    add("top", since="7d").add_argument("-n", dest="limit", type=int, default=10)
    add("name", device=True).add_argument("label")
    add("forget", device=True)
    add("export", since="1d").add_argument("-d", "--device")
    add("purge")
    add("status").add_argument("--check", action="store_true", help="exit 1 when the last cycle is older than 3 intervals")
    add("web").add_argument("--bind", help="HOST:PORT (default from config, 127.0.0.1:8778)")
    add("daemon")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # bars and names on a cp1252 console
    try:
        conf = cfg.load(a.config)
        if a.db:
            conf.db = a.db
        if a.cmd == "daemon":
            return cmd_daemon(conf)
        if a.cmd == "web":
            return cmd_web(a, conf)
        writes = a.cmd in ("name", "forget", "purge")
        with Store(conf.db, readonly=not writes) as store:
            if a.cmd in ("purge", "status"):
                return globals()[f"cmd_{a.cmd}"](store, a, conf)
            return globals()[f"cmd_{a.cmd}"](store, a)
    except (ValueError, LookupError, OSError, sqlite3.Error) as e:
        print(f"btlog: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            sys.stdout.flush()
        except OSError:
            pass
