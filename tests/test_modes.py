import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from btlog import cli, config, trends
from btlog.scanner import Advert
from btlog.store import FORGOTTEN, PRIVATE_KINDS, VERSION, Store
from helpers import BEACON, BTHOME, NRPA, PHONE, SENSOR, WATCH, adverts, at, thermometer

PHONE2 = "4A:00:00:00:00:02"    # another resolvable private address


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(map(str, args)))
    return code or 0, out.getvalue(), err.getvalue()


class ModeConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "btlog.toml"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return config.load(self.path)

    def keys(self, c):
        return {k: getattr(c, k) for k in config.MODES["home"]}

    def test_no_mode_is_home_is_todays_defaults(self):
        self.assertEqual(self.keys(config.Config()), config.MODES["home"])
        self.assertEqual((config.Config().mode, config.Config().retention_days, config.Config().forget_random_days),
                         ("home", 90, 7))
        self.assertEqual(self.keys(self.load("interval = 30\n")), config.MODES["home"])

    def test_each_mode_sets_its_keys(self):
        for mode, keys in config.MODES.items():
            with self.subTest(mode):
                c = self.load(f'mode = "{mode}"\n')
                self.assertEqual((c.mode, self.keys(c)), (mode, keys))
        c = self.load('mode = "record"\n')
        self.assertEqual((c.keep, c.payloads, c.retention_days, c.forget_random_days), ("all", True, 0, 0))
        self.assertEqual((self.load('mode = "footfall"\n').keep, self.load('mode = "footfall"\n').retention_days),
                         ("counts", 30))

    def test_a_key_in_the_file_beats_the_mode(self):
        c = self.load('mode = "sensors"\nretention_days = 365\n')
        self.assertEqual((c.keep, c.retention_days, c.forget_random_days), ("sensors", 365, 7))
        c = self.load('retention_days = 365\nmode = "record"\npayloads = false\n')   # order does not matter
        self.assertEqual((c.payloads, c.retention_days, c.forget_random_days), (False, 365, 0))
        c = self.load('keep = "counts"\nalerts = []\n')
        self.assertEqual((c.mode, c.keep, c.retention_days, c.alerts), ("home", "counts", 90, []))

    def test_alerts_are_a_copy_of_the_modes(self):
        c = self.load('mode = "sensors"\n')
        c.alerts.append("unusual")
        self.assertEqual(config.MODES["sensors"]["alerts"], ["silent", "range"])
        config.Config().alerts.append("unusual")
        self.assertEqual(config.Config().alerts, ["silent", "away"])

    def test_bad_values(self):
        for bad in ('mode = "office"\n', 'mode = 1\n', 'mode = ["home"]\n', 'keep = "none"\n', 'keep = ["all"]\n',
                    'payloads = 1\n', 'payloads = "true"\n', 'alerts = "silent"\n', 'alerts = [1]\n'):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)
        with self.assertRaisesRegex(ValueError, "home, sensors, footfall, record"):
            self.load('mode = "Home"\n')


class KeepSensorsTest(unittest.TestCase):
    def test_only_decoded_devices_are_written_but_every_cycle_counts(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        n = store.record_cycle(at(19, 8), [thermometer(20.0), *adverts((WATCH, -60), (PHONE, -50), (BEACON, -70))],
                               keep="sensors")
        self.assertEqual(n, 1)
        self.assertEqual(store.record_cycle(at(19, 8, 10), adverts((WATCH, -60)), keep="sensors"), 0)
        self.assertEqual([d["address"] for d in store.devices(private=True)], [SENSOR])
        self.assertEqual([s["address"] for s in store.sightings()], [SENSOR])
        self.assertEqual(store.conn.execute("SELECT COUNT(*) AS n FROM hourly").fetchone()["n"], 1)
        self.assertEqual(store.status()["cycles"], 2)
        # The sensor was heard in one of the two cycles of hour 8.
        self.assertEqual(trends.hours(store, store.find(SENSOR)["id"])[8], 0.5)
        with self.assertRaisesRegex(ValueError, "keep must be"):
            store.record_cycle(at(19, 9), [], keep="some")


class KeepCountsTest(unittest.TestCase):
    """The same scripted cycles recorded with keep "all" and keep "counts"."""

    CYCLES = [
        (at(19, 8), adverts((PHONE, -60), (WATCH, -50), (NRPA, -70))),
        (at(19, 8, 10), adverts((PHONE, -55), (PHONE, -65), (BEACON, -80))),
        # A sensor on a rotating address: counted, and its readings dropped with it.
        (at(19, 9), [*adverts((PHONE2, -40), (PHONE, -45), (WATCH, -60)),
                     Advert(PHONE2, -50, "random", service_data=thermometer(11.0).service_data)]),
        (at(19, 9, 10), []),
        (at(20, 9), adverts((NRPA, -75), (BEACON, -70))),
    ]

    def setUp(self):
        self.all, self.counts = Store(":memory:"), Store(":memory:")
        self.addCleanup(self.all.close)
        self.addCleanup(self.counts.close)
        self.recorded = [(self.all.record_cycle(ts, advs), self.counts.record_cycle(ts, advs, keep="counts"))
                         for ts, advs in self.CYCLES]

    def rows(self, store, sql):
        return [tuple(r.values()) for r in store.conn.execute(sql)]

    def everyone(self, store):
        return (trends.hours(store), trends.weekdays(store), [dict(r) for r in trends.timeline(store)],
                trends.hours(store, since=at(19, 9)), [dict(r) for r in trends.timeline(store, until=at(19, 9))])

    def test_no_rotating_address_is_ever_written(self):
        self.assertEqual(self.rows(self.counts, "SELECT address FROM devices WHERE kind IN ('rpa', 'nrpa')"), [])
        addresses = {r[0] for r in self.rows(self.counts, "SELECT address FROM devices")}
        self.assertEqual(addresses, {WATCH, BEACON, FORGOTTEN})
        self.assertEqual(self.rows(self.counts, """SELECT d.address FROM sightings s JOIN devices d ON d.id = s.device_id
                                                   WHERE d.kind IN ('rpa', 'nrpa', 'forgotten')"""), [])
        self.assertEqual(self.rows(self.counts, "SELECT * FROM readings"), [])
        self.assertEqual(self.counts.private_count(), 0)
        self.assertEqual([r[0] for r in self.recorded], [r[1] for r in self.recorded])  # devices recorded, folded too
        self.assertEqual(self.recorded[0], (3, 3))

    def test_fixed_addresses_are_recorded_as_usual(self):
        def fixed(store):
            return [(s["ts"], s["address"], s["rssi"], s["adverts"]) for s in store.sightings()
                    if s["kind"] not in PRIVATE_KINDS]
        self.assertEqual(fixed(self.counts), fixed(self.all))
        self.assertEqual(self.counts.find(WATCH)["cycles"], 2)
        self.assertEqual(trends.hours(self.counts, self.counts.find(BEACON)["id"]),
                         trends.hours(self.all, self.all.find(BEACON)["id"]))

    def test_all_device_trends_are_the_same(self):
        self.assertEqual(self.everyone(self.counts), self.everyone(self.all))
        self.assertEqual(trends.hours(self.counts)[8], 5 / 2)   # 3 devices, then 2, in two cycles
        # And they stay the same once keep "all" has forgotten its rotating
        # addresses: both end up with the same pseudo-device rows.
        self.assertEqual(self.all.purge(at(30, 12), 365, 7), (0, 3))
        self.assertEqual(self.counts.purge(at(30, 12), 365, 7), (0, 0))
        self.assertEqual(self.everyone(self.counts), self.everyone(self.all))
        sink = """SELECT h.day, h.hour, h.cycles, h.rssi_max, h.rssi_sum FROM hourly h JOIN devices d ON d.id = h.device_id
                  WHERE d.kind = 'forgotten' ORDER BY h.day, h.hour"""
        self.assertEqual(self.rows(self.counts, sink), self.rows(self.all, sink))
        self.assertEqual(self.rows(self.counts, sink)[0], ("2026-10-19", 8, 3, -55, -185))


class PayloadsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def payloads(self):
        return [tuple(r.values()) for r in self.store.conn.execute(
            """SELECT d.address, p.ts, p.field, p.data FROM payloads p JOIN devices d ON d.id = p.device_id
               ORDER BY d.address, p.ts, p.field""")]

    def tag(self, *mfr, service=None, address=WATCH):
        return Advert(address, -60, "random", manufacturer_data=dict(mfr), service_data=service or {})

    def test_last_bytes_of_each_field_per_cycle(self):
        self.store.record_cycle(at(19, 8), [
            self.tag((0x004C, b"\x02\x15"), (0x0499, b"\x05\x00")),
            self.tag((0x004C, b"\x10\x05"), service={"FCD2": b"\x40\x01\x61"}),
            *adverts((BEACON, -70))], payloads=True)
        self.store.record_cycle(at(19, 8, 1), [self.tag(service={BTHOME: b"\x40\x01\x60"})], payloads=True)
        self.assertEqual(self.payloads(), [
            (WATCH, at(19, 8), "m:004C", b"\x10\x05"), (WATCH, at(19, 8), "m:0499", b"\x05\x00"),
            (WATCH, at(19, 8), "s:" + BTHOME, b"\x40\x01\x61"), (WATCH, at(19, 8, 1), "s:" + BTHOME, b"\x40\x01\x60")])
        self.assertEqual(type(self.payloads()[0][3]), bytes)

    def test_off_by_default(self):
        self.store.record_cycle(at(19, 8), [self.tag((0x004C, b"\x02\x15"))])
        self.assertEqual(self.payloads(), [])

    def test_they_follow_retention_and_go_with_the_device(self):
        for ts in (at(1, 8), at(20, 8)):
            self.store.record_cycle(ts, [self.tag((0x004C, b"\x01")), self.tag((0x004C, b"\x02"), address=PHONE)],
                                    payloads=True)
        self.assertEqual(self.store.purge(at(20, 12), retention_days=0, forget_random_days=0), (0, 0))
        self.assertEqual(len(self.payloads()), 4)
        self.store.purge(at(20, 12), retention_days=10, forget_random_days=0)
        self.assertEqual([(a, ts) for a, ts, _, _ in self.payloads()], [(PHONE, at(20, 8)), (WATCH, at(20, 8))])
        self.store.purge(at(30, 12), retention_days=365, forget_random_days=7)    # the phone's address is forgotten
        self.store.forget(WATCH)
        self.assertEqual(self.payloads(), [])

    def test_sightings_carry_them_as_hex_when_asked(self):
        self.store.record_cycle(at(19, 8), [self.tag((0x004C, b"\xab\x01"), service={"fcd2": b"\x40"}),
                                            *adverts((BEACON, -70))], payloads=True)
        rows = list(self.store.sightings(payloads=True))
        self.assertEqual([r.get("payloads") for r in rows], [None, {"m:004C": "ab01", "s:" + BTHOME: "40"}])
        self.assertNotIn("payloads", rows[0])
        self.assertNotIn("payloads", next(iter(self.store.sightings())))


class FileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db, self.toml = Path(tmp.name) / "btlog.db", Path(tmp.name) / "btlog.toml"
        with Store(self.db) as store:
            store.record_cycle(at(19, 8), [Advert(WATCH, -60, "random", "Watch", 0x004C, {0x004C: b"\x12\x19\x00"}),
                                           *adverts((BEACON, -70))], payloads=True)

    def test_export_json_shows_payloads_as_hex(self):
        code, out, err = run_cli("--db", self.db, "export", "--since", "all", "--json")
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)
        self.assertEqual([(r["address"], r.get("payloads")) for r in rows], [(BEACON, None), (WATCH, {"m:004C": "121900"})])
        self.assertNotIn("payloads", rows[0])
        _, out, _ = run_cli("--db", self.db, "export", "--since", "all")
        self.assertEqual(out.splitlines()[0], "ts,time,address,kind,name,label,rssi,adverts")
        self.assertEqual(out.splitlines()[2].split(",")[2:], [WATCH, "static", "Watch", "", "-60", "1"])

    def test_status_shows_the_mode(self):
        self.toml.write_text('mode = "sensors"\n', encoding="utf-8")
        _, out, _ = run_cli("--config", self.toml, "--db", self.db, "--json", "status")
        self.assertEqual({k: json.loads(out)[k] for k in ("mode", "keep", "payloads")},
                         {"mode": "sensors", "keep": "sensors", "payloads": False})
        _, out, _ = run_cli("--config", self.toml, "--db", self.db, "status")
        self.assertEqual(out.splitlines()[1], "mode       sensors (keep sensors)")
        self.toml.write_text('mode = "record"\n', encoding="utf-8")
        _, out, _ = run_cli("--config", self.toml, "--db", self.db, "status")
        self.assertEqual(out.splitlines()[1], "mode       record (keep all, raw payloads)")
        self.assertIn("(kept ever days)", out)

    def test_reader_on_a_version_2_file_has_no_payloads(self):
        with Store(self.db) as old:
            old.conn.executescript("DROP TABLE payloads; PRAGMA user_version = 2;")
        with Store(self.db, readonly=True) as r:
            self.assertEqual((r.has_readings, r.has_payloads), (True, False))
            self.assertEqual([s.get("payloads") for s in r.sightings(payloads=True)], [None, None])
        code, out, err = run_cli("--db", self.db, "export", "--since", "all", "--json")
        self.assertEqual((code, err, len(json.loads(out))), (0, "", 2))
        with Store(self.db) as w:
            self.assertEqual(w.conn.execute("PRAGMA user_version").fetchone()["user_version"], VERSION)
            w.record_cycle(at(19, 9), [Advert(WATCH, -60, "random", manufacturer_data={0x004C: b"\x01"})], payloads=True)
        with Store(self.db, readonly=True) as r:
            self.assertEqual([s.get("payloads") for s in r.sightings(payloads=True)], [None, None, {"m:004C": "01"}])


if __name__ == "__main__":
    unittest.main()
