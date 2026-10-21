# btlog

A bluesitting daemon. Wardriving goes out to find Bluetooth devices; btlog stays
in situ, day and night, and notes which Bluetooth LE devices come by, when, and
how strongly. Then it answers the trend questions: who is around at which hours
and on which days, who is new this week, who is here the most, and whether a
device is drifting closer or further away.

The scanner records every advertisement it hears into a SQLite database. You
look at the result either on the command line (`btlog hours "my watch"`) or on a
read-only web page (`btlog web`), whichever you prefer; both read the same
database and the same config file. Python 3.11+, one dependency
([bleak](https://github.com/hbldh/bleak), for BlueZ over D-Bus), a NixOS module.

## What it does

| | |
|---|---|
| **Scans** | Every `interval` seconds (60 by default) it listens for BLE advertisements and records one sighting per device: time, address, address kind, name, manufacturer id, strongest RSSI, number of advertisements. |
| **Rolls up** | Each sighting also advances a per-device, per-local-hour rollup and a count of scan cycles per hour. Trends read the rollups, so they are instant and survive the raw data being purged. |
| **Measures against uptime** | Presence is a share of the scan cycles that actually ran in that hour, not of the wall clock: a day the scanner was off does not look like a day nobody was home. |
| **Forgets rotating addresses** | Phones and watches change their random address every few minutes. Such an address unseen for `forget_random_days` (7) is removed; only its contribution to "how many devices per hour" is kept, under one anonymous pseudo-device. |
| **Keeps raw data a while** | Raw sightings are kept `retention_days` (90) for CSV export and debugging. Hourly rollups are tiny and kept forever. |
| **Names things** | A device without a name is still shown by vendor (Apple, Garmin, Samsung...) from its manufacturer data, and you can label any device (`btlog name`). |
| **CLI** | `devices`, `show`, `hours`, `days`, `heat`, `rssi`, `new`, `top`, `name`, `export`, `status`; bars in a terminal, `--json` for scripts. |
| **Web** | `btlog web`: devices, presence by hour and weekday, a day-by-hour heatmap, signal per day, new devices on one page, standard library only. |

## Command line

```bash
btlog devices                          # devices heard, newest sighting first
btlog devices --private --since 7d     # rotating private addresses too, seen this week
btlog name C0:AA:BB:CC:DD:EE "my watch"
btlog show "my watch"                  # when it is around, by hour of day and weekday
btlog hours                            # how many devices are around at each hour
btlog days "my watch" --since 90d
btlog heat "my watch" --since 14d      # presence calendar: one row per day, one cell per hour
btlog rssi "my watch" --by hour        # signal strength over time (max / average dBm)
btlog new --since 1d                   # devices seen for the first time since yesterday
btlog top --since 7d                   # devices around the most
btlog forget "neighbour's tv"          # remove one device and everything recorded about it
btlog export --since 2d > sightings.csv
btlog export --since 2h --json
btlog status --check                   # exit 1 when the daemon has not scanned for 3 intervals
```

A device is an address, a label, an advertised name or an unambiguous prefix of
one. `--since` takes `30m`, `2h`, `3d`, `today`, `yesterday`, `2026-10-18`,
`2026-10-18T19:30` or `all`; the trend commands cut at the hour it falls in.
`--json`, `--db` and `--config` go before or after the subcommand.

```
$ btlog hours "my watch"
presence by hour of day for C0:AA:BB:CC:DD:EE my watch since 30d (share of scan cycles)
 00  ████████████████████  100%
 01  ████████████████████  100%
 ...
 08  ███████▌              37%
 09                        0%
 ...
 18  ██████████████▎       71%

$ btlog heat "my watch" --since 7d
presence of C0:AA:BB:CC:DD:EE my watch since 7d, one row per day, one cell per hour
            0  3  6  9  12 15 18 21
2026-10-14  ████▇█▇█  ····    █▇███▇
2026-10-15  ████████          ██▇█▇▇
2026-10-17  ██▇▇▇▇██▇▇███▇███▇▇███▇█
```

A `·` is an hour the scanner did not run; a blank cell is an hour it ran and
did not hear the device.

Address kinds: `public` and `static` addresses identify a device for good;
`rpa` (resolvable private) and `nrpa` (non-resolvable private) rotate every few
minutes (one phone leaves about a hundred a day), so they are hidden from
`devices` unless asked for and are the ones that get forgotten; `unknown` is what a backend that does not report the
address type gives (bleak on Windows), and is kept like `public`.

## Web dashboard

```bash
btlog web                          # http://127.0.0.1:8778
btlog web --bind 0.0.0.0:8778      # reachable from the network
```

One dark page, no frameworks: the devices on the left; for the selected device
(or all of them) presence by hour of day and by weekday, a day-by-hour heatmap,
signal strength per day, and the new devices of the last week. The same data is
served as JSON under `/api/` (`status`, `devices`, `hours`, `days`, `timeline`,
`rssi`, `new`, each taking `since` and `device` where it makes sense).

The dashboard opens the database read-only and never writes. Device names come
from strangers' advertisements, so the page escapes everything before it goes
into HTML. **It has no authentication.** Leave it on localhost and reach it over an SSH port forward
(`ssh -L 8778:127.0.0.1:8778 bench`) or Tailscale, or bind it to a trusted
network only.

## Configuration

`/etc/btlog.toml` (what the NixOS module writes), or `$BTLOG_CONFIG`, or
`--config`; every key has a default:

```toml
db = "/var/lib/btlog/btlog.db"   # default ~/.local/state/btlog/btlog.db
interval = 60                    # seconds per scan cycle
adapter = "hci0"                 # default: the default adapter
retention_days = 90              # raw sightings
forget_random_days = 7           # rotating private addresses
web_bind = "127.0.0.1:8778"
```

## Database

SQLite, WAL mode so the CLI and the dashboard read while the daemon writes.

| table | one row per | kept |
|---|---|---|
| `devices` | address: kind, name, label, manufacturer id, first and last seen, sightings | until forgotten (private kinds only) |
| `sightings` | device and scan cycle: strongest RSSI, advertisements heard | `retention_days` |
| `hourly` | device and local hour: cycles heard, best and summed RSSI | forever |
| `cycles` | local hour: scan cycles the daemon ran | forever |

Hours are local time, because "who is here at eight in the morning" is a local
question. Privacy: the database is readable by the `btlog` user only
(`/var/lib/btlog`, mode 0750); `forget_random_days` bounds how long a rotating
address is kept; `btlog forget` removes one device, `btlog export` takes data
out, and deleting the database file starts over. Advertised names are stored
with control characters stripped, so a hostile name cannot drive your terminal.

## NixOS

```nix
# flake.nix
inputs.btlog.url = "github:lavrentiy/btlog";
inputs.btlog.inputs.nixpkgs.follows = "nixpkgs";

# in the system's modules
btlog.nixosModules.default
{
  services.btlog = {
    enable = true;
    interval = 60;
    retentionDays = 90;
    forgetRandomDays = 7;
    web.enable = true;       # dashboard on http://127.0.0.1:8778, no authentication
  };
}
```

The module enables `hardware.bluetooth` and runs the scanner as its own `btlog`
user with a hardened systemd unit that has no network at all (`PrivateNetwork`),
only the system D-Bus socket to reach BlueZ. The daemon logs one summary line
per hour; `BTLOG_DEBUG=1` in the environment logs every cycle. The dashboard is a second unit under the same user
(SQLite's WAL needs readers to share the writer's `-shm` file) with the database
opened read-only.

Other Linux distributions: `pip install .`, then run `btlog daemon` under
systemd as a user that may use BlueZ. Windows and macOS: bleak scans there too,
but the address type is not reported, so every device is `unknown` and nothing
is ever forgotten; the CLI and the dashboard work the same.

## Development

```bash
nix develop                                   # or any Python 3.11+ with bleak
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python tools/demo.py demo.db   # ten days of made-up sightings, no adapter needed
PYTHONPATH=src python -m btlog --db demo.db heat "my watch"
PYTHONPATH=src python -m btlog --db demo.db web
nix build                                     # builds and runs the tests
```

The tests need no Bluetooth hardware and do not import bleak: the daemon loop
runs against a scripted scanner source (cycles, an empty cycle, a failing scan),
the store and the trend queries against in-memory databases with days of
synthetic sightings, the CLI through `main()`, and the dashboard served on a
free port from a temporary database.
