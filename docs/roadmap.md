# Roadmap: from a log to a tool

btlog records who comes by and how often. That is interesting to look at but
rarely something you act on. This plan adds the parts that make it useful:
readings from your own BLE sensors, alerts when something changes, and
modes that set btlog up for one job: a home, a shed full of sensors, a shop
that counts visitors, or a log kept as evidence.

The current shape stays, because it works:

- **One dependency.** bleak plus the standard library. Decoding is done
  with `struct`, notifications with `urllib`. Anything that needs more
  (Victron's encrypted readout needs AES) is left out.
- **Rollups.** Every new measure gets an hourly rollup next to its raw rows,
  with retention for the raw rows only. Trends stay instant after years.
- **Presence against uptime.** Readings and counts are shares or averages
  of the cycles the scanner actually ran.
- **Private by default.** Anything that looks at other people's devices is
  off unless a mode turns it on, and the README says what that mode means.
- **Every phase ships on its own.** You can stop after any of them and have
  a better tool than before.

## Modes

A mode is a named set of defaults for the existing and new config keys.
It is not a separate code path: `mode = "sensors"` means the same as
writing the keys it implies, and any key in the file still overrides it.
So there is one daemon, one schema and one dashboard, and the tests cover
the keys rather than every mode.

| mode | for | stores | alerts by default | rotating addresses |
|---|---|---|---|---|
| `home` (default) | your place | every device, sensor readings | sensor silent, labelled device away | counted, forgotten after 7 days |
| `sensors` | a shed, a van, a greenhouse | decoded sensors only; everything else is dropped before it is written | sensor silent, reading out of range | never stored |
| `footfall` | a shop or office | fixed-address devices as in `home`; rotating ones only as counts per hour, no address at all | none | counted, never stored |
| `record` | evidence after a break-in | everything, raw payloads too, kept forever | none | kept forever |

`home` is the default: today's behaviour plus phases 1 and 3, and it
shows something on day one even without sensors. `record` took over the
README's former "keeping everything" section, and its legal note moved to
the mode.

The keys a mode sets:

| key | values | home | sensors | footfall | record |
|---|---|---|---|---|---|
| `keep` | `all`, `sensors`, `counts` | all | sensors | counts | all |
| `payloads` | true, false | false | false | false | true |
| `retention_days` | days, 0 = forever | 90 | 90 | 30 | 0 |
| `forget_random_days` | days, 0 = never | 7 | 7 | 7 | 0 |
| `alerts` | list of rules | silent, away | silent, range | none | none |

What `keep` does in `record_cycle()`:

- `all`: as today.
- `sensors`: drops adverts that no decoder turned into a reading. The
  cycle is still counted, so "silent" alerts still know the scanner was
  running.
- `counts`: rotating addresses are never written to `devices`. Their
  device-cycles go straight into the existing "forgotten" pseudo-device's
  hourly row, the same row `purge()` folds into today. So `btlog hours`
  and the dashboard's all-device charts work unchanged, and no address of
  a passer-by ever touches the disk.

Config and NixOS:

```toml
mode = "sensors"
retention_days = 365      # overrides the mode's 90
notify = "https://ntfy.sh/my-shed-7f3a"
```

```nix
services.btlog = { enable = true; mode = "sensors"; notify = "https://ntfy.sh/my-shed-7f3a"; };
```

## Phase 1 (done): payloads, decoders, readings

Today `advert_from()` keeps only the lowest company id. The new pieces:

- **`Advert`** carries `manufacturer_data` and `service_data` as bytes
  (bleak's `AdvertisementData` has both).
- **`decoders.py`**: functions from (company id or service UUID, bytes) to
  `{kind: value}`, for example `{"temperature": 21.4, "humidity": 48.0,
  "battery": 87}`. A registry dict, one small function per format, and
  each one is tested against the published sample payloads of its format.
  - BTHome v2, unencrypted (service data 0xFCD2). An open format used by
    Shelly BLU and many DIY sensors, so one decoder covers many devices.
    Start here.
  - Ruuvi RAWv2, data format 5 (manufacturer 0x0499). Well documented,
    with test vectors.
  - Xiaomi LYWSD03MMC with the pvvx/ATC custom firmware (service data
    0x181A). The stock firmware encrypts its data, so it is out.
  - Later, if asked for: Govee H5075, SwitchBot meters. Out: Victron
    Instant Readout and encrypted BTHome, which need AES.
- **Schema**, the same pattern as RSSI:

  ```sql
  CREATE TABLE readings (            -- raw, retention_days like sightings
      ts REAL NOT NULL, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, value REAL NOT NULL,
      PRIMARY KEY (device_id, kind, ts)) WITHOUT ROWID;
  CREATE TABLE hourly_readings (     -- kept forever
      device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
      day TEXT NOT NULL, hour INTEGER NOT NULL, weekday INTEGER NOT NULL, kind TEXT NOT NULL,
      n INTEGER NOT NULL, min REAL NOT NULL, max REAL NOT NULL, sum REAL NOT NULL,
      PRIMARY KEY (device_id, kind, day, hour)) WITHOUT ROWID;
  ```

  A sensor with three kinds adds about 4,300 raw rows a day at a 60 s
  interval: roughly 390,000 rows (about 15 MB) at 90 days. The rollup is
  72 rows a day.
- **`PRAGMA user_version`** gets its first use: version 1 is today's
  schema, version 2 adds these tables. `Store.__init__` migrates forward
  with `CREATE TABLE IF NOT EXISTS`.
- **CLI**: `btlog readings DEVICE [--kind temperature] [--by hour|day]`
  (min, average and max with a bar), and `btlog now`: what was heard in
  the last cycle, with RSSI and latest readings.
- **Dashboard**: a sensor panel, with one line of hourly averages per kind
  and a min–max band.

Roughly 400 lines with tests.

## Phase 2 (done): modes, `keep` and raw payloads

- `config.py`: `mode`, `keep` and `payloads`. `MODES` is a dict of
  defaults; `load()` starts from the mode and applies the file over it.
  Unknown modes are an error, like unknown keys.
- `record_cycle()` applies `keep` as described above.
- With `payloads = true`, the exact bytes of every manufacturer and
  service data field go into their own table, one row per device, cycle
  and field (the last value heard in the cycle). Evidence then does not
  depend on what the decoders understood, and a decoder written later can
  be run over old data. Raw payloads follow `retention_days` and roughly
  double the database in `record` mode.

  ```sql
  CREATE TABLE payloads (
      ts REAL NOT NULL, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
      field TEXT NOT NULL,           -- 'm:004C' (company id) or 's:0000fcd2-...' (service UUID)
      data BLOB NOT NULL,
      PRIMARY KEY (device_id, ts, field)) WITHOUT ROWID;
  ```

  `btlog export --json` includes them as hex.
- The module gets a `mode` enum option, and the README gets a "Modes"
  section with the legal note per mode.
- Tests: `keep = "counts"` never writes a rotating address, yet the
  all-device hours are the same as with `all`; `keep = "sensors"` stores
  only decoded devices but still counts the cycle.

As built, where it differs or adds to the above:

- The schema is `user_version` 3. A read-only store on a version-2 file
  exports without payloads until the daemon has migrated it.
- `keep = "counts"` also drops a rotating address's readings and payloads:
  folded into one anonymous device they mean nothing.
- A service UUID field is always the full 128-bit lower-case UUID, short
  ones expanded onto the Bluetooth base, so one service has one name.
- `btlog export --json` gives each sighting that has payloads a
  `"payloads": {field: hex}` object; the CSV keeps its columns.
- `alerts` is a config key now (a list of rule names), so that phase 3
  has its defaults in place; nothing reads it yet.
- The NixOS module's `retentionDays` and `forgetRandomDays` became
  `nullOr` with a default of null, and `payloads` joined them: options that
  always landed in `/etc/btlog.toml` would beat the mode's defaults. There
  is no `keep` option: it is what the mode is for.

Roughly 200 lines with tests.

## Phase 3 (done): rules and notifications

- **`rules.py`**: each rule is a function over the store that returns the
  alerts that hold now.
  - `silent`: a device with readings, or a labelled device, not heard for
    N hours.
  - `away`: a labelled device disappears; the alert opens when it leaves
    and closes when it is back, with a debounce of a few cycles so a weak
    signal does not flap. One rule rather than `arrived` and `left`, as
    the open/close pair already says both.
  - `range`: a reading outside the bounds set in the config file, per
    device label. The config file, not the database, so that bounds are
    declarative (a NixOS option) and the dashboard stays read-only:

    ```toml
    [range.freezer]
    temperature = [-30, -15]
    battery = [20, 100]
    ```
  - `unusual`: a known device in an hour of day where its presence over
    the last 30 days is under 2 %.
- **`alerts` table**: `(rule, device_id, opened, closed, sent)`. One
  notification when an alert opens and one when it closes; no repeats.
- **`notify`**: an `https://` URL gets a POST (ntfy takes the body as the
  message; anything else gets JSON). A `command:` prefix runs a program
  with the alert as JSON on stdin. That is all: email goes through a
  command (`command:/usr/bin/mail -s btlog me@example.org`), so no mail
  password ever sits in btlog's config.
- **Where it runs**: not in the scanner. The scanner unit has no network
  (`PrivateNetwork`) and should keep it that way. A separate
  `btlog alerts` command evaluates the rules and sends. NixOS runs it from
  a timer every few minutes, as the btlog user, with network allowed. The
  only thing it writes is the `alerts` table, so it holds SQLite's write
  lock for milliseconds.
- **CLI**: `btlog alerts` (list open and recent), `btlog alerts --run`
  (what the timer calls), `btlog alerts --test` (send one test
  notification).
- **API**: `/api/present` (labelled devices heard in the last N minutes)
  and `/api/alerts`, for Home Assistant or a shell script.

As built, where it differs or adds to the above:

- Only labelled devices are watched, by every rule; see Decisions. So
  `silent` is a labelled device with readings, and a labelled device
  without readings is `away`'s.
- `away` debounces by time, `away_minutes` (15), not by a count of cycles;
  `silent_hours` (6) is the other new key, and `/api/present` uses the
  away minutes too.
- A fifth rule, `scanner`, always on: no cycle for 3 intervals (the test of
  `btlog status --check`). While it is open, silent, away and range are not
  evaluated and their episodes stay as they are, since absence cannot be
  told from a dead scanner. `unusual` still runs: it fires on a device heard.
- `range` counts only readings from the last `silent_hours`; one episode per
  device and kind. The config is checked at load (kinds from
  `decoders.UNITS`, `low <= high`); a label no device has is reported by each
  `--run`, as only the database knows labels.
- `unusual` also needs the device known for 7 days. Consecutive unusual hours
  are one episode, and its close is notified like any other.
- `notify` has three explicit forms rather than guessing from the URL: a
  plain http(s) URL is ntfy (the text), `webhook:URL` gets the JSON,
  `command:` the JSON on stdin. Checked at load.
- Schema `user_version` 4. `alerts` has one row per episode: rule,
  device_id (NULL for scanner, ON DELETE CASCADE), key (the kind for range),
  opened, closed, message, `sent_open` and `sent_closed` (delivery times),
  and a partial unique index allowing one open episode per alert.
- Delivery: a failure is reported on stderr and retried on the next run; the
  first failure ends a run's sending, so a dead target costs one timeout.
  An episode that closed before its open notification got through is never
  sent: it is over, and the list still has it. A target set later gets what
  is open then, not the backlog. `--run` exits 0 even then: the table is
  current and the next run retries.
- A rule taken out of `alerts` loses its open episodes, without a
  notification; they would otherwise stay open for good.
- NixOS: `notify`, `alerts` (an enum list), `silentHours`, `awayMinutes`,
  `range`, and the `btlog-alerts` timer, on whenever the module is. The unit
  is ordered after `network.target`, not `network-online.target`: a box
  without network must still keep the table for the dashboard.

Roughly 350 lines with tests.

## Footfall

There is no phase of its own: phase 2's `keep = "counts"` plus a "busy"
view (this week against the last four, per hour of day) on the existing
all-device counts. The README must be honest about it: this is a proxy.
Phones advertise more or less depending on make, settings and whether the
screen is on, so it shows trends ("Saturdays are twice as busy as
Tuesdays"), not head counts.

## Size and testing

Code grows from about 1,300 to about 2,250 lines; tests grow alongside.
Everything stays testable without hardware: decoders against published
payloads, the store and rules against in-memory databases, notifications
against a local HTTP server on a free port, and the alerts timer as a
plain function call. What only the bench can show: how often real sensors
advertise, whether BlueZ passes service data through unchanged, and
whether a paired phone really shows up under one address.

## Decisions

- **Watching: opt-in per device, by label.** A label (`btlog name`) means
  "this one is mine", and only labelled devices raise alerts. A stranger's
  device never does: btlog logs who comes by, it does not keep watch on
  other people.
- **Default mode: `home`.** It works on day one without sensors. `sensors`
  is one line away for anyone who wants nothing about strangers stored.
- **Notifications: an https POST (ntfy or any webhook) and `command:`.**
  Email goes through a command, which keeps mail credentials out of the
  config.
- **Raw payloads: stored in `record` mode only** (`payloads = true`), for
  evidence and for re-decoding later.
- **Range bounds: in the config file, per device label**, so they are
  declarative and the dashboard stays read-only.
- **Tracker detection: not in btlog.** It is the one feature that looks
  for a specific kind of other people's devices, so it belongs in a tool of
  its own, if it is built at all. Notes for that tool: Apple Find My
  "offline finding" adverts (manufacturer 0x004C, type 0x12) mark a tag
  away from its owner. Such a tag keeps one address for about a day, which
  makes it detectable from a fixed place. The layout must be checked
  against public research first. It would reuse btlog's decoders and
  rules as a library.
