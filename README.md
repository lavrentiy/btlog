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
| **Scans** | Every `interval` seconds (60 by default) it listens for BLE advertisements and records one sighting per device: time, address, address kind, name, manufacturer id, strongest RSSI, number of reports from the Bluetooth stack. |
| **Rolls up** | Each sighting also advances a per-device, per-local-hour rollup and a count of scan cycles per hour. Trends read the rollups, so they are instant and survive the raw data being purged. |
| **Measures against uptime** | Presence is a share of the scan cycles that actually ran in that hour, not of the wall clock: a day the scanner was off does not look like a day nobody was home. |
| **Forgets rotating addresses** | Phones and watches change their random address every few minutes. Such an address unseen for `forget_random_days` (7 by default) is removed; only its contribution to "how many devices per hour" is kept, under one anonymous pseudo-device. |
| **Keeps raw data a while** | Raw sightings are kept `retention_days` (90 by default) for CSV export and debugging. Hourly rollups are tiny and kept forever. |
| **Names things** | A device without a name is still shown by vendor (Apple, Garmin, Samsung...) from its manufacturer data, and you can label any device (`btlog name`). |
| **Reads sensors** | Thermometers and the like that put their readings in their advertisements (BTHome, Ruuvi, Xiaomi on custom firmware) are decoded as they are heard: temperature, humidity, battery and more, with the same hourly rollups. See [Sensors](#sensors). |
| **Alerts** | For the devices you labelled, and only those: a sensor gone silent, a reading out of range, your watch leaving and coming back, a device at an unusual hour, and a scanner that stopped. To ntfy, a webhook or any command (email). See [Alerts](#alerts). |
| **Modes** | One line sets it up for a job: `home` (the default), `sensors` (nothing about strangers stored), `footfall` (rotating addresses only counted) or `record` (everything, raw payloads too, kept forever). See [Modes](#modes). |
| **CLI** | `devices`, `show`, `hours`, `days`, `heat`, `rssi`, `readings`, `now`, `new`, `top`, `name`, `export`, `status`, `alerts`; bars in a terminal, `--json` for scripts. |
| **Web** | `btlog web`: devices, presence by hour and weekday, a day-by-hour heatmap, signal and sensor readings per day, new devices and open alerts on one page, standard library only. |

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
btlog readings garden --kind temperature --by hour   # sensor readings: min / average / max
btlog now                              # the last scan cycle: who is here, and the latest readings
btlog new --since 1d                   # devices seen for the first time since yesterday
btlog top --since 7d                   # devices around the most
btlog forget "neighbour's tv"          # remove one device and everything recorded about it
btlog export --since 2d > sightings.csv
btlog export --since 2h --json         # with the raw payloads as hex, where they are stored
btlog export --since 2026-10-01 --until 2026-11-01 > october.csv
btlog backup /mnt/nas/btlog-$(date +%F).db   # consistent copy, safe while the daemon writes
btlog status --check                   # exit 1 when the daemon has not scanned for 3 intervals
btlog alerts                           # open alerts and the last 20 closed ones
btlog alerts --run                     # evaluate the rules once and notify (what the timer runs)
btlog alerts --test                    # one test notification to the notify target
```

A device is an address, a label, an advertised name or an unambiguous prefix of
one. `--since` and `--until` take `30m`, `2h`, `3d`, `today`, `yesterday`,
`2026-10-18`, `2026-10-18T19:30` or `all`; the trend commands cut at the hour
they fall in.
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
2026-10-16  ························
2026-10-17  ██▇▇▇▇██▇▇███▇███▇▇███▇█
```

A `·` is an hour the scanner did not run; a blank cell is an hour it ran and
did not hear the device. Two devices with the same label are an error, not a
guess; names and labels match case-insensitively in any script.

Address kinds: `public` and `static` addresses identify a device for good;
`rpa` (resolvable private) and `nrpa` (non-resolvable private) rotate every few
minutes (one phone leaves about a hundred a day), so they are hidden from
`devices` unless asked for and are the ones that get forgotten; `unknown` is what a backend that does not report the
address type gives (bleak on Windows), and is kept like `public`.

## Sensors

Many BLE sensors broadcast their readings in every advertisement, so btlog
reads them without connecting or pairing. It decodes:

- **BTHome v2**, unencrypted (Shelly BLU H&T and many DIY sensors);
- **Ruuvi** RuuviTag, data format 5;
- **Xiaomi LYWSD03MMC** and kin, but only with the
  [pvvx](https://github.com/pvvx/ATC_MiThermometer) or atc1441 custom
  firmware: the stock firmware encrypts its readings.

Encrypted formats (encrypted BTHome, Victron Instant Readout) are left out,
as they need AES, which the standard library does not have. Nothing needs
configuring: a sensor in range simply gets readings next to its sightings.

```
$ btlog readings garden --kind temperature --since 2026-10-19
readings of 38:39:8F:00:00:01 garden by day since 2026-10-19 (min / average / max)
2026-10-19  temperature  █████████▅               6.05 /   11.41 /   16.86 °C
2026-10-20  temperature  ██████████▁              6.25 /   11.69 /   17.21 °C
2026-10-21  temperature  █████                    6.54 /    8.81 /   13.75 °C
```

Each cycle stores the last value of each kind a sensor sent. Those raw
readings follow `retention_days` like sightings; the hourly rollup (count,
min, max and sum per kind) is kept forever like presence, so `btlog readings`
and the dashboard give the same answer after the raw rows are gone. `btlog
now` shows each sensor's latest reading with its age, since a sensor need not
advertise in every cycle.

## Alerts

`btlog alerts --run` evaluates a few rules over the database and sends one
notification when an alert opens and one when it closes. It runs outside the
scanner, from a timer every five minutes in the NixOS module, because the
scanner has no network and should keep it that way.

**Watching is opt-in, by label.** Only a device you labelled with `btlog name`
can raise an alert: the label is how you say "this one is mine". A stranger's
phone that stops coming by raises nothing, and btlog keeps no watch on other
people's devices.

| rule | opens when | closes when |
|---|---|---|
| `silent` | a labelled sensor (a device with readings) is not heard for `silent_hours` (6) | it is heard again |
| `away` | any other labelled device is not heard for `away_minutes` (15): "my watch left" | it is heard again: "my watch is back" |
| `range` | the latest reading of a kind, from the last `silent_hours`, is outside the bounds set for that label under `[range.LABEL]`; one alert per device and kind | the reading is back inside |
| `unusual` | a labelled device is heard in an hour of day it was around less than 2 % of the time over the last 30 days (once btlog has known it a week) | it is no longer heard in an hour unusual for it |
| `scanner` | the scanner has not run a cycle for 3 intervals (the test of `btlog status --check`) | cycles resume |

`alerts` in the config picks the rules: the mode's are silent and away in
`home`, silent and range in `sensors`; `unusual` is on only when you add it.
`scanner` is always on. While the scanner is down, silent, away and range are
not evaluated at all: absence cannot be told from a dead scanner, so you get
one "scanner has not run since ..." rather than every device leaving at once.
The away minutes are the debounce: a watch at the edge of range misses a
cycle now and then without having left.

Notifications go to `notify`, in one of three forms:

- `https://ntfy.sh/btlog-7f3a9c2e` (any http or https URL): an
  [ntfy](https://ntfy.sh) topic; the phone shows the message, titled with the rule.
- `webhook:https://ha.local/api/webhook/btlog`: the alert as JSON, `{"rule",
  "state": "open" or "closed", "device", "name", "message", "ts"}`, for Home
  Assistant and the like.
- `command:PROGRAM ARGS`: the same JSON on stdin. That is how email works,
  with no mail password in btlog's config:
  `command:/usr/bin/mail -s btlog me@example.org`, or msmtp.

Each episode is notified once when it opens and once when it closes, never
more. A delivery that fails is reported on stderr (the journal, under the
timer) and tried again on the next run. An episode that closed before its
open notification ever got through is not sent at all: it is over, and it
stays listed in `btlog alerts`. Without `notify`, `--run` still keeps the
table, so the dashboard shows the open alerts, and setting `notify` later
sends what is open then, not the backlog. `btlog alerts --test` sends one test
alert and exits 1 with the reason when it cannot.

```
$ btlog alerts
OPEN SINCE        RULE    MESSAGE
2026-10-21 07:42  silent  freezer not heard since 2026-10-21 01:40

recently closed:
OPENED            CLOSED            RULE  MESSAGE
2026-10-21 08:05  2026-10-21 17:58  away  my watch left
```

Mind who can read the target: on ntfy.sh, knowing a topic's name is all it
takes to read your alerts and to post fake ones, and on NixOS the config is
world-readable (see [NixOS](#nixos)). Use a long random topic, or a
self-hosted ntfy with access control.

## Web dashboard

```bash
btlog web                          # http://127.0.0.1:8778
btlog web --bind 0.0.0.0:8778      # reachable from the network
```

One dark page, no frameworks: the devices on the left; for the selected device
(or all of them) presence by hour of day and by weekday, a day-by-hour heatmap,
signal strength per day, a chart per reading for a sensor, and the new devices
of the last week; open alerts above the charts and their count in the header.
The same data is served as JSON under `/api/` (`status`, `devices`, `hours`,
`days`, `timeline`, `rssi`, `readings`, `now`, `new`, each taking `since`,
`until` and `device` where it makes sense; `readings` also takes `kind` and
`by=day|hour`), plus `alerts` (open and the last 20 closed, as
`btlog --json alerts`) and `present` (labelled devices heard within
`away_minutes`, for Home Assistant or a script).

The dashboard opens the database read-only and never writes. Device names come
from strangers' advertisements, so the page escapes everything before it goes
into HTML. It answers to IP addresses, `localhost`, the bind address and,
when bound beyond localhost, the machine's own hostname, FQDN and
`hostname.local`; add other names (a Tailscale MagicDNS name) to `web_names`.
Any other name gets a 421, so a web page you happen to have open cannot rebind
its name to 127.0.0.1 and read when the house is empty. **It has no authentication.** Leave it on localhost and reach it over an SSH port forward
(`ssh -L 8778:127.0.0.1:8778 bench`) or Tailscale, or bind it to a trusted
network only.

## Modes

A mode sets btlog up for one job. It is a named set of defaults for a few
config keys, not a separate code path: `mode = "sensors"` means the same as
writing the keys it implies, and any of those keys written in the config file
beats the mode's value.

| mode | for | stores | rotating addresses |
|---|---|---|---|
| `home` (default) | your place | every device, sensor readings; raw rows for 90 days | counted, forgotten after 7 days |
| `sensors` | a shed, a van, a greenhouse | decoded sensors only; everything else is dropped before it is written | not stored (unless one is a sensor) |
| `footfall` | a shop or office | devices with a fixed address as in `home`, rotating ones only as a count per hour; raw rows for 30 days | counted, never stored |
| `record` | evidence after a break-in | everything, raw payloads too, kept forever | kept forever |

The keys a mode sets:

| key | values | home | sensors | footfall | record |
|---|---|---|---|---|---|
| `keep` | `all`, `sensors`, `counts` | all | sensors | counts | all |
| `payloads` | true, false | false | false | false | true |
| `retention_days` | days, 0 = forever | 90 | 90 | 30 | 0 |
| `forget_random_days` | days, 0 = never | 7 | 7 | 7 | 0 |
| `alerts` | list of rule names | silent, away | silent, range | none | none |

`keep = "sensors"` drops every device that no decoder got a reading from in
that scan cycle, before anything is written; the cycle itself still counts,
so presence stays measured against uptime. `keep = "counts"` never writes a
rotating private address: each one heard adds to the per-hour count of the
anonymous pseudo-device that `forget_random_days` folds old addresses into
anyway, so `btlog hours` and the dashboard's all-device charts come out the
same as with `all` (its readings, meaningless once anonymous, are dropped).
`payloads = true` stores the exact bytes of every manufacturer and service
data field, the last ones heard per device and cycle, so evidence does not
depend on what the decoders understood and a decoder written later can be run
over old data; `btlog export --json` gives them as hex, and they roughly
double the database. `alerts` names the rules `btlog alerts --run` evaluates;
see [Alerts](#alerts).

What a mode means for other people's devices:

- **home**: the neighbours' and passers-by's devices are logged with yours;
  rotating addresses go after a week, fixed ones (headphones, cars, beacons)
  stay until you `btlog forget` them.
- **sensors**: nothing about other people's devices is stored, unless one
  happens to be a sensor that btlog decodes.
- **footfall**: no visitor's rotating address touches the disk, but the count
  is a proxy. Phones advertise more or less depending on make, settings and
  whether the screen is on, so it shows trends ("Saturdays are twice as busy
  as Tuesdays"), not head counts. And not every device rotates: headphones,
  fitness bands and cars often keep one address, and those are stored by
  address for 30 days, as in `home`.
- **record**: a log to hand over after a break-in, with
  `btlog export --since 2026-10-17 --until 2026-10-18 --json` as the output
  (raw payloads included), `sha256sum` on the file as the seal, and
  `btlog backup` for the nightly copy (a plain file copy of a database in WAL
  mode can tear). The CSV form is for people and spreadsheets: a name that
  starts like a formula gets a leading apostrophe there, so prefer `--json`
  for the sealed copy. Know what you are getting: a rotating address still
  cannot be tied to a person without that person's device, a signal strength
  does not say inside or outside, and logging the devices of everyone who
  walks past, forever, is processing their personal data, which in the EU
  needs a legal basis, a retention limit and a notice, as CCTV does. btlog
  gives you the log; the rest is yours.

## Configuration

`/etc/btlog.toml` (what the NixOS module writes), or `$BTLOG_CONFIG`, or
`--config`. Every key has a default; `mode` sets some of them (see
[Modes](#modes)), and a key written here beats the mode:

```toml
mode = "sensors"                 # home (default), sensors, footfall or record
db = "/var/lib/btlog/btlog.db"   # default ~/.local/state/btlog/btlog.db
interval = 60                    # seconds per scan cycle
adapter = "hci0"                 # default: the default adapter
retention_days = 365             # overrides the mode's 90; 0 keeps raw rows forever
# forget_random_days = 7         # the mode's; rotating private addresses, 0 never forgets
# keep = "sensors"               # the mode's: all, sensors or counts
# payloads = false               # the mode's; true stores the raw advertisement bytes too
# alerts = ["silent", "range"]   # the mode's rules; add "unusual" to have it
notify = "https://ntfy.sh/btlog-7f3a9c2e"  # or webhook:URL or command:PROGRAM ARGS; default: none
silent_hours = 6                 # a labelled sensor unheard this long is silent
away_minutes = 15                # a labelled device unheard this long has left
web_bind = "127.0.0.1:8778"
web_names = []                   # extra Host names, e.g. ["bench.tailnet-1234.ts.net"]

[range.freezer]                  # bounds per device label, for the range rule
temperature = [-30, -15]
battery = [20, 100]
```

The config is checked when it is loaded: an unknown rule, a `notify` in none
of the three forms, or a range with an unknown kind or `low > high` stops
btlog with a one-line error. A range label that no device has is reported by
every `btlog alerts --run`, as only the database knows the labels.

`btlog status` shows the mode in effect.

## Database

SQLite, WAL mode so the CLI and the dashboard read while the daemon writes.

| table | one row per | kept |
|---|---|---|
| `devices` | address: kind, name, label, manufacturer id, first and last seen, sightings | until forgotten (private kinds only) |
| `sightings` | device and scan cycle: strongest RSSI, advertisements heard | `retention_days` |
| `hourly` | device and local hour: cycles heard, best and summed RSSI | forever |
| `cycles` | local hour: scan cycles the daemon ran | forever |
| `readings` | sensor, kind and scan cycle: the value heard | `retention_days` |
| `hourly_readings` | sensor, kind and local hour: count, min, max and sum | forever |
| `payloads` | device, scan cycle and field (`m:004C` company id, `s:` + service UUID): the raw bytes, with `payloads = true` only | `retention_days` |
| `alerts` | alert episode: rule, device (none for `scanner`), kind for `range`, opened, closed, message, and when the open and the close notification were delivered | forever; a device's go when it is forgotten |

Hours are local time, because "who is here at eight in the morning" is a local
question. `PRAGMA user_version` is the schema version (2 since readings, 3
since raw payloads, 4 since alerts); the daemon (or `btlog alerts --run`)
migrates the file when it starts, and a dashboard or CLI that meets an older
file shows no readings, payloads or alerts until then. `btlog alerts --run`
writes the alerts table and nothing else. Privacy: the database is readable by the `btlog` user only
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
    mode = "home";           # or sensors, footfall, record; see Modes
    interval = 60;
    # retentionDays = 365;   # left unset: the mode's 90
    web.enable = true;       # dashboard on http://127.0.0.1:8778, no authentication
    notify = "https://ntfy.sh/btlog-7f3a9c2e";   # readable by anyone on the box, see below
    range.freezer.temperature = [ (-30) (-15) ];
  };
}
```

`retentionDays`, `forgetRandomDays`, `payloads`, `alerts`, `silentHours` and
`awayMinutes` default to `null`, which leaves them to the mode (or btlog's
default); set one and it wins over the mode, as in the config file. `range` is
written only when set; `notify` defaults to none.

Alerts run from the `btlog-alerts` timer: `btlog alerts --run` five minutes
after boot and every five minutes after that, as the `btlog` user, in a
oneshot unit of its own with the same hardening as the scanner's but network
access; the scanner keeps `PrivateNetwork`. The timer is on whenever the
module is, `notify` or not, as the alerts table feeds the dashboard.
**`notify` lands in the world-readable Nix store and in `/etc/btlog.toml`**:
anyone with a shell on the machine can read it, and an ntfy.sh topic is all
it takes to read your alerts or post fake ones. Use a long random topic, or a
self-hosted ntfy with access control reached through a `command:` whose
script reads its token from a file only the btlog user can read. That unit
has no new privileges, so a mailer that needs a setgid helper (Postfix's
`sendmail`) fails from it; msmtp works.

The module enables `hardware.bluetooth` and runs the scanner as its own `btlog`
user with a hardened systemd unit that has no network at all (`PrivateNetwork`),
only the system D-Bus socket to reach BlueZ. The database belongs to that user
alone, so on the box run the CLI as `sudo -u btlog btlog devices` (or use the
dashboard). The daemon logs one summary line
per hour; `BTLOG_DEBUG=1` in the environment logs every cycle. The dashboard is a second unit under the same user
(SQLite's WAL needs readers to share the writer's `-shm` file) with the database
opened read-only.

On a board without a battery-backed clock (a Raspberry Pi, say), set
`waitForTimeSync = true`: cycles logged before NTP has set the clock land under
the wrong hours and stay in the rollups. The catch is the reverse case: a
machine that never gets network then never starts scanning, and other units
ordered after `time-sync.target` wait too. To cap the wait, also set
`systemd.services.systemd-time-wait-sync.serviceConfig.TimeoutStartSec = "5min";`
and scanning starts after five minutes either way.

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
PYTHONPATH=src python -m btlog --db demo.db readings garden
PYTHONPATH=src python -m btlog --db demo.db web
nix build                                     # builds and runs the tests
```

The tests need no Bluetooth hardware and do not import bleak: the daemon loop
runs against a scripted scanner source (cycles, an empty cycle, a failing scan),
the store and the trend queries against in-memory databases with days of
synthetic sightings, the CLI through `main()`, the dashboard served on a
free port from a temporary database, the alert rules over scripted cycles at
a chosen time, and notifications against a local HTTP server.
