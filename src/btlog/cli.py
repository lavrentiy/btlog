"""btlog: an always-on log of Bluetooth LE devices in proximity, with trends.

  btlog devices [--since 7d]       devices heard, newest sighting first (--private adds rotating ones)
  btlog show DEVICE                one device: when it is around, by hour and weekday
  btlog hours [DEVICE]             presence per hour of day (all devices: how many)
  btlog days [DEVICE]              presence per weekday
  btlog rssi DEVICE [--by hour]    signal strength over time
  btlog new [--since 1d]           devices seen for the first time recently
  btlog top [--since 7d]           devices around the most
  btlog name DEVICE LABEL          label a device ("" removes the label)
  btlog export [--since 1d]        raw sightings as CSV
  btlog purge                      apply retention now (the daemon does it hourly)
  btlog status                     database size and the last scan cycle
  btlog web                        read-only dashboard on http://127.0.0.1:8778 (no authentication)
  btlog daemon                     the scanner itself (runs as a service)

DEVICE is an address, a label, a name or an unambiguous address prefix.
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time

from btlog import __version__, config as cfg, trends, when
from btlog.store import Store

BAR = " ▁▂▃▄▅▆▇█"


# ---------- Output helpers ----------

def bar(value, scale, width=20):
    """A horizontal bar of width cells for value out of scale, in eighths."""
    if value is None or not scale:
        return "·" * width if value is None else " " * width
    eighths = round(value / scale * width * 8)
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
    """Label, else name, else nothing: how a device is called in tables."""
    return d["label"] or d["name"] or ""


def device_row(d):
    return [d["address"], d["kind"], who(d), when.fmt(d["first_seen"]), when.fmt(d["last_seen"]), d["hours"]]


def public(d):
    """A device as JSON: times as unix seconds, nothing internal."""
    return {k: v for k, v in d.items() if k != "id"}


def presence_lines(values, labels, scale=None, fmt="{:.0%}"):
    scale = scale or max((v for v in values if v is not None), default=0)
    return "\n".join(f"{lab:>3}  {bar(v, scale)}  {'' if v is None else fmt.format(v)}".rstrip()
                     for lab, v in zip(labels, values))


HOURS = [f"{h:02d}" for h in range(24)]


# ---------- Commands ----------

def cmd_devices(store, a):
    since = when.parse(a.since) if a.since else None
    rows = store.devices(since, private=a.private)
    text = table([device_row(d) for d in rows], ["ADDRESS", "KIND", "LABEL/NAME", "FIRST SEEN", "LAST SEEN", "HOURS"])
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
        f"{d['cycles']} sightings, manufacturer id {d['manufacturer'] if d['manufacturer'] is not None else '-'}",
        "", f"presence by hour of day since {a.since}:", presence_lines(hours, HOURS),
        "", "by weekday:", presence_lines(days, trends.WEEKDAYS)]))


def _presence(store, a, fn, labels, title):
    since = when.parse(a.since)
    if a.device:
        d = store.find(a.device)
        values = fn(store, d["id"], since)
        text = f"{title} for {d['address']} {who(d)} since {a.since} (share of scan cycles)\n"
        text += presence_lines(values, labels)
    else:
        values = fn(store, None, since)
        text = f"{title} since {a.since} (average devices heard per scan cycle)\n"
        text += presence_lines(values, labels, fmt="{:.1f}")
    emit(a, values, text)


def cmd_hours(store, a):
    _presence(store, a, trends.hours, HOURS, "presence by hour of day")


def cmd_days(store, a):
    _presence(store, a, trends.weekdays, trends.WEEKDAYS, "presence by weekday")


def cmd_rssi(store, a):
    d = store.find(a.device)
    rows = trends.rssi(store, d["id"], when.parse(a.since), a.by)
    # RSSI is negative dBm; bars grow from -100 towards 0 so nearer reads as longer.
    text = f"signal of {d['address']} {who(d)} by {a.by} since {a.since} (dBm, max / average)\n"
    text += "\n".join(f"{r['when']}  {bar(r['avg'] + 100, 100)}  {r['max']:4d} / {r['avg']:6.1f}" for r in rows)
    emit(a, rows, text)


def cmd_new(store, a):
    rows = trends.new(store, when.parse(a.since))
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


def cmd_export(store, a):
    since = when.parse(a.since)
    did = store.find(a.device)["id"] if a.device else None
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["time", "address", "kind", "name", "label", "rssi", "adverts"])
    for s in store.sightings(since, did):
        out.writerow([when.fmt(s["ts"], seconds=True), s["address"], s["kind"], s["name"] or "",
                      s["label"] or "", s["rssi"], s["adverts"]])


def cmd_purge(store, a, conf):
    sightings, devices = store.purge(time.time(), conf.retention_days, conf.forget_random_days)
    print(f"deleted {sightings} sightings older than {conf.retention_days} days, "
          f"forgot {devices} private addresses unseen for {conf.forget_random_days} days")


def cmd_status(store, a, conf):
    s = store.status()
    emit(a, s, "\n".join([
        f"database   {conf.db}",
        f"devices    {s['devices']} (plus {s['private']} rotating private addresses)",
        f"sightings  {s['sightings']} (kept {conf.retention_days} days)",
        f"cycles     {s['cycles']} since {s['first_day'] or '-'}",
        f"last cycle {when.fmt(s['last_cycle'], seconds=True)}"]))


def cmd_web(a, conf):
    from btlog.web import serve
    serve(conf.db, a.bind or conf.web_bind)


def cmd_daemon(conf):
    from btlog.scanner import BleakSource, run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    # SystemExit unwinds through asyncio.run, which stops discovery cleanly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with Store(conf.db) as store:
        logging.getLogger("btlog").info("scanning every %gs into %s", conf.interval, conf.db)
        run(BleakSource(conf.adapter), store, conf)


# ---------- Main ----------

def parser():
    p = argparse.ArgumentParser(prog="btlog", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"btlog {__version__}")
    p.add_argument("--config", help="TOML config (default $BTLOG_CONFIG or /etc/btlog.toml)")
    p.add_argument("--db", help="database file, overriding the config")
    p.add_argument("--json", action="store_true", help="JSON output for scripts")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, since=None, device=False):
        s = sub.add_parser(name, help=name)
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
    s = add("rssi", since="7d", device=True)
    s.add_argument("--by", choices=["day", "hour"], default="day")
    add("new", since="1d")
    add("top", since="7d").add_argument("-n", dest="limit", type=int, default=10)
    add("name", device=True).add_argument("label")
    add("export", since="1d").add_argument("-d", "--device")
    add("purge")
    add("status")
    add("web").add_argument("--bind", help="HOST:PORT (default from config, 127.0.0.1:8778)")
    add("daemon")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        conf = cfg.load(a.config)
        if a.db:
            conf.db = a.db
        if a.cmd == "daemon":
            return cmd_daemon(conf)
        if a.cmd == "web":
            return cmd_web(a, conf)
        writes = a.cmd in ("name", "purge")
        with Store(conf.db, readonly=not writes) as store:
            if a.cmd in ("purge", "status"):
                return globals()[f"cmd_{a.cmd}"](store, a, conf)
            return globals()[f"cmd_{a.cmd}"](store, a)
    except (ValueError, LookupError, OSError) as e:
        print(f"btlog: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            sys.stdout.flush()
        except OSError:
            pass
