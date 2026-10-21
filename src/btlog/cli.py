"""btlog: a bluesitting daemon. It sits in situ all day and night, logs the
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
  btlog backup FILE                consistent copy of the database
  btlog purge                      apply retention now (the daemon does it hourly)
  btlog status [--check]           database size and the last scan cycle (--check: exit 1 if stale)
  btlog web                        read-only dashboard on http://127.0.0.1:8778 (no authentication)
  btlog daemon                     the scanner itself (runs as a service)

DEVICE is an address, a label, a name or an unambiguous prefix of one.
--since and --until take 30m, 2h, 3d, today, yesterday, 2026-10-18, 2026-10-18T19:30 or all.
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

from datetime import date, timedelta

from btlog import __version__, config as cfg, trends, vendors, when
from btlog.store import Store, public

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


def cell(value, scale):
    """One character of a heatmap: a dot where the scanner did not run."""
    if value is None:
        return "·"
    return BAR[round(min(value, scale) / scale * 8)] if scale else " "


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


def presence(values, labels, device):
    """One line per slot. A device's presence is a share and is drawn against
    100%, so a device around 20% of the time gets a short bar; device counts
    are drawn against their maximum."""
    if device:
        scale, fmt = 1.0, "{:.0%}"
    else:
        scale, fmt = max((v for v in values if v is not None), default=0), "{:.1f}"
    return "\n".join(f"{lab:>3}  {bar(v, scale)}  {'' if v is None else fmt.format(v)}".rstrip()
                     for lab, v in zip(labels, values))


def period(a):
    """(since, until) from the flags, as unix seconds or None."""
    return when.parse(a.since), when.parse(a.until) if a.until else None


def period_text(a):
    return f"since {a.since}" + (f" until {a.until}" if a.until else "")


# ---------- Commands ----------

def cmd_devices(store, a):
    since, until = period(a)
    rows = store.devices(since, until, private=a.private)
    text = table([device_row(d) for d in rows], ["ADDRESS", "KIND", "LABEL/NAME", "VENDOR", "FIRST SEEN", "LAST SEEN", "HOURS"])
    if not a.private:
        text += f"\n({store.private_count(since, until)} rotating private addresses not shown; --private lists them)"
    emit(a, [public(d) for d in rows], text)


def cmd_show(store, a):
    d = store.find(a.device)
    since, until = period(a)
    hours, days = trends.hours(store, d["id"], since, until), trends.weekdays(store, d["id"], since, until)
    emit(a, {**public(d), "hours": hours, "weekdays": days}, "\n".join([
        f"{d['address']}  {d['kind']}  {who(d)}".rstrip(),
        f"first seen {when.fmt(d['first_seen'])}, last seen {when.fmt(d['last_seen'])}, "
        f"{d['cycles']} sightings, vendor {vendors.name(d['manufacturer']) or '-'}",
        "", f"presence by hour of day {period_text(a)} (share of scan cycles):", presence(hours, HOURS, d),
        "", "by weekday:", presence(days, trends.WEEKDAYS, d)]))


def _presence(store, a, fn, labels, title):
    d = store.find(a.device) if a.device else None
    values = fn(store, d["id"] if d else None, *period(a))
    text = (f"{title} for {d['address']} {who(d)}".rstrip() + f" {period_text(a)} (share of scan cycles)" if d
            else f"{title} {period_text(a)} (average devices heard per scan cycle)")
    emit(a, values, text + "\n" + presence(values, labels, d))


def cmd_hours(store, a):
    _presence(store, a, trends.hours, HOURS, "presence by hour of day")


def cmd_days(store, a):
    _presence(store, a, trends.weekdays, trends.WEEKDAYS, "presence by weekday")


def cmd_heat(store, a):
    """One row per day, one cell per hour: a presence calendar. Every day from
    the first to the last gets a row, so a day the scanner was off is a row of
    dots rather than a missing line."""
    d = store.find(a.device) if a.device else None
    rows = trends.timeline(store, d["id"] if d else None, *period(a))
    seen = {}
    for r in rows:
        seen.setdefault(r["day"], [None] * 24)[r["hour"]] = r["presence"]
    days = {}
    if seen:
        first, last = date.fromisoformat(min(seen)), date.fromisoformat(max(seen))
        for n in range((last - first).days + 1):
            day = (first + timedelta(days=n)).isoformat()
            days[day] = seen.get(day, [None] * 24)
    if d:
        title, scale = f"presence of {d['address']} {who(d)}".rstrip(), 1.0
    else:
        scale = max((r["presence"] for r in rows), default=0)
        title = f"devices heard per scan cycle (full cell = {scale:.1f})"
    lines = [f"{title} {period_text(a)}, one row per day, one cell per hour",
             "            " + "".join(f"{h:<3d}" for h in range(0, 24, 3))]
    lines += [f"{day}  " + "".join(cell(v, scale) for v in cells) for day, cells in days.items()]
    emit(a, [dict(r) for r in rows], "\n".join(lines))


def cmd_rssi(store, a):
    d = store.find(a.device)
    rows = trends.rssi(store, d["id"], *period(a), by=a.by)
    # RSSI is negative dBm; bars grow from -100 towards 0 so nearer reads as longer.
    text = f"signal of {d['address']} {who(d)} by {a.by} {period_text(a)} (dBm, max / average)\n"
    text += "\n".join(f"{r['when']}  {bar(max(r['avg'] + 100, 0), 100)}  {r['max']:4d} / {r['avg']:6.1f}" for r in rows)
    emit(a, rows, text)


def cmd_new(store, a):
    since, until = period(a)
    if since is None:
        raise ValueError("new needs a --since other than all")
    rows = trends.new(store, since, until)
    emit(a, [public(d) for d in rows],
         table([[d["address"], d["kind"], who(d), when.fmt(d["first_seen"]), d["cycles"]] for d in rows],
               ["ADDRESS", "KIND", "LABEL/NAME", "FIRST SEEN", "SIGHTINGS"]))


def cmd_top(store, a):
    rows = trends.busiest(store, *period(a), limit=a.limit)
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


def csv_text(text):
    """A name or label as a CSV cell that a spreadsheet will not run: one that
    starts like a formula (=, +, -, @) gets a leading apostrophe, the usual
    way to force text. Names come from strangers' advertisements."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def cmd_export(store, a):
    """Sightings as CSV: unix seconds and local time, so the hour that repeats
    when daylight saving ends is still unambiguous. --json gives the rows as
    they are, for a sealed copy."""
    did = store.find(a.device)["id"] if a.device else None
    rows = store.sightings(*period(a), device_id=did)
    if a.json:
        return print(json.dumps([dict(s) for s in rows], indent=2))
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["ts", "time", "address", "kind", "name", "label", "rssi", "adverts"])
    for s in rows:
        out.writerow([int(s["ts"]), when.fmt(s["ts"], seconds=True), s["address"], s["kind"], csv_text(s["name"]),
                      csv_text(s["label"]), s["rssi"], s["adverts"]])


def cmd_backup(store, a):
    store.backup(a.file)
    print(f"database copied to {a.file}")


def cmd_purge(store, a, conf):
    sightings, devices = store.purge(time.time(), conf.retention_days, conf.forget_random_days)
    print(f"deleted {sightings} sightings older than {conf.retention_days or 'ever'} days, "
          f"forgot {devices} private addresses unseen for {conf.forget_random_days or 'ever'} days")


def cmd_status(store, a, conf):
    s = store.status()
    stale = s["last_cycle"] is None or time.time() - s["last_cycle"] > 3 * conf.interval
    size = sum(os.path.getsize(f) for f in (str(conf.db), f"{conf.db}-wal") if os.path.exists(f))
    emit(a, {**s, "stale": stale, "bytes": size}, "\n".join([
        f"database   {conf.db} ({size / 1e6:.1f} MB)",
        f"devices    {s['devices']} (plus {s['private']} rotating private addresses)",
        f"sightings  {s['sightings']} (kept {conf.retention_days or 'ever'} days)",
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
    # SystemExit ends the process without unwinding the scan coroutine. That is
    # fine: BlueZ stops a client's discovery when its D-Bus connection closes.
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
            s.add_argument("--until", help="same forms; the end of the period (default: now)")
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
    add("backup").add_argument("file")
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
