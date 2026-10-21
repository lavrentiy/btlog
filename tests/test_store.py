import tempfile
import unittest
from pathlib import Path

from btlog.store import FORGOTTEN, Store, address_kind, after, local_hour, printable
from helpers import BEACON, NRPA, PHONE, WATCH, adverts, at, weekday


class AddressKindTest(unittest.TestCase):
    def test_kinds_from_address_type_and_top_bits(self):
        self.assertEqual(address_kind(BEACON, "public"), "public")
        self.assertEqual(address_kind(WATCH, "random"), "static")
        self.assertEqual(address_kind(PHONE, "random"), "rpa")
        self.assertEqual(address_kind(NRPA, "random"), "nrpa")
        self.assertEqual(address_kind("80:00:00:00:00:00", "random"), "unknown")  # 10.. is reserved
        self.assertEqual(address_kind(PHONE, None), "unknown")                    # backend didn't say
        self.assertEqual(address_kind("zz", "random"), "unknown")

    def test_local_hour(self):
        self.assertEqual(local_hour(at(19, 8, 30)), ("2026-10-19", 8, weekday(19)))

    def test_after_cuts_rollups_at_the_hour(self):
        self.assertEqual(after(None), ("1", ()))
        self.assertEqual(after(at(19, 8, 30), "c"),
                         ("(c.day > ? OR (c.day = ? AND c.hour >= ?))", ("2026-10-19", "2026-10-19", 8)))

    def test_printable(self):
        self.assertEqual(printable("evil\x1b[2J\nname\t"), "evil[2Jname")  # no ESC, no sequence
        self.assertEqual(printable("Zoë's Watch"), "Zoë's Watch")
        self.assertIsNone(printable("\x07"))
        self.assertIsNone(printable(None))


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_cycle_keeps_strongest_rssi_and_counts_adverts(self):
        n = self.store.record_cycle(at(19, 8), adverts((PHONE, -70), (PHONE, -55), (PHONE, -80, "Pixel"), (BEACON, -90)))
        self.assertEqual(n, 2)
        rows = {s["address"]: s for s in self.store.sightings()}
        self.assertEqual(rows[PHONE]["rssi"], -55)
        self.assertEqual(rows[PHONE]["adverts"], 3)
        self.assertEqual(rows[PHONE]["name"], "Pixel")
        self.assertEqual(rows[PHONE]["kind"], "rpa")
        self.assertEqual(rows[BEACON]["adverts"], 1)

    def test_device_keeps_name_and_manufacturer_once_learned(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60, "Watch", 0x004C)))
        self.store.record_cycle(at(19, 9), adverts((WATCH, -65)))
        d = self.store.find(WATCH)
        self.assertEqual((d["name"], d["manufacturer"], d["cycles"]), ("Watch", 0x004C, 2))
        self.assertEqual((d["first_seen"], d["last_seen"]), (at(19, 8), at(19, 9)))

    def test_hourly_rollup_and_cycle_count(self):
        for minute in (0, 10, 20):
            self.store.record_cycle(at(19, 8, minute), adverts((WATCH, -60 - minute)))
        self.store.record_cycle(at(19, 8, 30), [])
        self.store.record_cycle(at(19, 9, 0), adverts((WATCH, -50)))
        hourly = self.store.conn.execute("SELECT * FROM hourly ORDER BY hour").fetchall()
        self.assertEqual([(h["day"], h["hour"], h["weekday"], h["cycles"], h["rssi_max"], h["rssi_sum"]) for h in hourly],
                         [("2026-10-19", 8, weekday(19), 3, -60, -210), ("2026-10-19", 9, weekday(19), 1, -50, -50)])
        cycles = self.store.conn.execute("SELECT hour, count FROM cycles ORDER BY hour").fetchall()
        self.assertEqual([(c["hour"], c["count"]) for c in cycles], [(8, 4), (9, 1)])

    def test_devices_listing_filters(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (PHONE, -60)))
        self.store.record_cycle(at(20, 8), adverts((BEACON, -60)))
        self.assertEqual([d["address"] for d in self.store.devices(private=True)], [BEACON, PHONE, WATCH])
        self.assertEqual([d["address"] for d in self.store.devices(since=at(20, 0))], [BEACON])
        self.assertEqual([d["address"] for d in self.store.devices()], [BEACON, WATCH])
        self.assertEqual(self.store.devices()[0]["hours"], 1)

    def test_find_by_address_prefix_label_name(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60, "Garmin"), (PHONE, -60), (BEACON, -60)))
        self.store.set_label(WATCH, "my watch")
        self.assertEqual(self.store.find("garmin")["address"], WATCH)
        self.assertEqual(self.store.find("MY WATCH")["address"], WATCH)
        self.assertEqual(self.store.find("my w")["address"], WATCH)
        self.assertEqual(self.store.find("c0:aa")["address"], WATCH)
        self.assertNotIn("exact", self.store.find("garmin"))
        self.store.set_label(BEACON, "Garmin")  # exact label beats the name prefix
        self.assertEqual(self.store.find("garmin")["address"], BEACON)
        with self.assertRaisesRegex(LookupError, "matches 2"):
            self.store.find("garm")
        self.assertEqual(self.store.find("5")["address"], PHONE)
        with self.assertRaises(LookupError):
            self.store.find("ZZ")
        with self.assertRaisesRegex(LookupError, "matches 2"):
            self.store.record_cycle(at(19, 9), adverts(("C0:FF:00:00:00:00", -60)))
            self.store.find("c0:")
        with self.assertRaises(LookupError):
            self.store.set_label("FF:FF:FF:FF:FF:FF", "x")

    def test_status(self):
        self.assertEqual(self.store.status()["cycles"], 0)
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60)))
        s = self.store.status()
        self.assertEqual((s["devices"], s["private"], s["sightings"], s["cycles"], s["last_cycle"], s["first_day"]),
                         (1, 0, 1, 1, at(19, 8), "2026-10-19"))
        # A quiet cycle still counts as the last cycle: a dead daemon and a
        # quiet room must not look the same.
        self.store.record_cycle(at(19, 23, 50), [])
        self.assertEqual(self.store.status()["last_cycle"], at(19, 23, 50))

    def test_kind_upgrades_from_unknown_only(self):
        from btlog.scanner import Advert
        self.store.record_cycle(at(19, 8), [Advert(WATCH, -60)])
        self.assertEqual(self.store.find(WATCH)["kind"], "unknown")
        self.store.record_cycle(at(19, 9), adverts((WATCH, -60)))
        self.assertEqual(self.store.find(WATCH)["kind"], "static")
        self.store.record_cycle(at(19, 10), [Advert(WATCH, -60)])
        self.assertEqual(self.store.find(WATCH)["kind"], "static")

    def test_forget_one_device(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (BEACON, -70)))
        self.store.forget(WATCH)
        self.assertEqual([d["address"] for d in self.store.devices()], [BEACON])
        self.assertEqual(self.store.status()["cycles"], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM hourly").fetchone()["n"], 1)
        with self.assertRaises(LookupError):
            self.store.forget(WATCH)


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        # Day 1: a phone (rotating address) and the watch. Day 20: the watch alone.
        self.store.record_cycle(at(1, 8), adverts((PHONE, -60), (WATCH, -60), (NRPA, -70)))
        self.store.record_cycle(at(1, 9), adverts((PHONE, -50), (WATCH, -60)))
        self.store.record_cycle(at(20, 8), adverts((WATCH, -60)))

    def test_old_sightings_go_but_rollups_stay(self):
        sightings, devices = self.store.purge(at(20, 12), retention_days=10, forget_random_days=365)
        self.assertEqual((sightings, devices), (5, 0))
        self.assertEqual([s["address"] for s in self.store.sightings()], [WATCH])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM hourly").fetchone()["n"], 6)

    def test_forgetting_private_addresses_keeps_the_counts(self):
        sightings, devices = self.store.purge(at(20, 12), retention_days=365, forget_random_days=7)
        self.assertEqual((sightings, devices), (0, 2))
        self.assertEqual([d["address"] for d in self.store.devices(private=True)], [WATCH])
        f = self.store.forgotten()
        self.assertEqual((f["kind"], f["cycles"], f["first_seen"]), (FORGOTTEN, 3, at(1, 8)))
        folded = self.store.conn.execute(
            "SELECT hour, cycles, rssi_max, rssi_sum FROM hourly WHERE device_id = ? ORDER BY hour", (f["id"],)).fetchall()
        self.assertEqual([tuple(r.values()) for r in folded], [(8, 2, -60, -130), (9, 1, -50, -50)])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM sightings").fetchone()["n"], 3)
        # A second purge with nothing to forget leaves the pseudo-device alone,
        # and one with a new stale address folds into it.
        self.assertEqual(self.store.purge(at(21, 12), 365, 7), (0, 0))
        self.assertEqual(self.store.forgotten()["cycles"], 3)
        self.store.record_cycle(at(21, 8), adverts(("4A:00:00:00:00:02", -40)))
        self.assertEqual(self.store.purge(at(30, 12), 365, 7), (0, 1))
        f = self.store.forgotten()
        self.assertEqual((f["cycles"], f["first_seen"]), (4, at(1, 8)))
        folded = self.store.conn.execute(
            "SELECT day, cycles, rssi_max FROM hourly WHERE device_id = ? AND hour = 8 ORDER BY day", (f["id"],)).fetchall()
        self.assertEqual([tuple(r.values()) for r in folded], [("2026-10-01", 2, -60), ("2026-10-21", 1, -40)])

    def test_recently_seen_private_address_survives(self):
        self.store.record_cycle(at(20, 9), adverts((PHONE, -60)))
        self.assertEqual(self.store.purge(at(20, 12), 365, 7), (0, 1))
        self.assertEqual({d["address"] for d in self.store.devices(private=True)}, {WATCH, PHONE})


class FileTest(unittest.TestCase):
    def test_readonly_open_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "btlog.db"
            with self.assertRaises(FileNotFoundError):
                Store(path, readonly=True)
            with Store(path) as w:
                w.record_cycle(at(19, 8), adverts((WATCH, -60)))
                with Store(path, readonly=True) as r:
                    self.assertEqual(len(r.devices()), 1)
                    with self.assertRaises(Exception):
                        r.set_label(WATCH, "x")


if __name__ == "__main__":
    unittest.main()
