import tempfile
import unittest
from pathlib import Path

from btlog import trends
from btlog.scanner import Advert
from btlog.store import VERSION, Store
from helpers import BEACON, PHONE, SENSOR, WATCH, adverts, at, bthome, thermometer

# The sample payloads of test_decoders: the BTHome spec's example advertisement
# (25.00 °C, 50.55 %) and the "valid data" vector of Ruuvi's data format 5.
BTHOME_SAMPLE = "4002C40903BF13"
RUUVI_SAMPLE = "0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F"
RUUVI_TAG = "C8:25:2D:8E:9C:2C"


def ruuvi_tag():
    return Advert(RUUVI_TAG, -80, "random", None, 0x0499, {0x0499: bytes.fromhex(RUUVI_SAMPLE)})


class RecordReadingsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def rows(self, sql):
        return [tuple(r.values()) for r in self.store.conn.execute(sql)]

    def readings(self):
        return self.rows("""SELECT d.address, r.ts, r.kind, r.value FROM readings r JOIN devices d ON d.id = r.device_id
                            ORDER BY d.address, r.kind, r.ts""")

    def hourly(self):
        return self.rows("SELECT hour, kind, n, min, max, sum FROM hourly_readings ORDER BY kind, day, hour, device_id")

    def test_readings_and_rollups_from_real_payloads(self):
        n = self.store.record_cycle(at(19, 8), [bthome(SENSOR, BTHOME_SAMPLE), ruuvi_tag(), *adverts((WATCH, -60))])
        self.assertEqual(n, 3)
        self.assertEqual(self.readings(), [
            (SENSOR, at(19, 8), "humidity", 50.55), (SENSOR, at(19, 8), "temperature", 25.0),
            (RUUVI_TAG, at(19, 8), "humidity", 53.49), (RUUVI_TAG, at(19, 8), "pressure", 1000.44),
            (RUUVI_TAG, at(19, 8), "temperature", 24.3), (RUUVI_TAG, at(19, 8), "voltage", 2.977)])
        self.assertEqual(self.hourly()[:2], [(8, "humidity", 1, 50.55, 50.55, 50.55), (8, "humidity", 1, 53.49, 53.49, 53.49)])
        self.assertEqual(len(self.hourly()), 6)
        self.assertEqual(self.store.find(RUUVI_TAG)["manufacturer"], 0x0499)

    def test_last_value_of_a_kind_wins_within_a_cycle(self):
        # Battery comes in a packet of its own and survives the later temperature.
        self.store.record_cycle(at(19, 8), [thermometer(20.0), bthome(SENSOR, "400161"), thermometer(21.5)])
        self.store.record_cycle(at(19, 8, 10), [thermometer(19.0)])
        self.store.record_cycle(at(19, 9), [thermometer(23.0)])
        self.assertEqual(self.readings(), [
            (SENSOR, at(19, 8), "battery", 97.0), (SENSOR, at(19, 8), "temperature", 21.5),
            (SENSOR, at(19, 8, 10), "temperature", 19.0), (SENSOR, at(19, 9), "temperature", 23.0)])
        self.assertEqual(self.hourly(), [(8, "battery", 1, 97.0, 97.0, 97.0),
                                         (8, "temperature", 2, 19.0, 21.5, 40.5), (9, "temperature", 1, 23.0, 23.0, 23.0)])
        self.assertEqual(self.store.find(SENSOR)["cycles"], 3)

    def test_purge_drops_old_readings_but_keeps_the_rollups(self):
        self.store.record_cycle(at(1, 8), [thermometer(10.0)])
        self.store.record_cycle(at(20, 8), [thermometer(20.0)])
        self.assertEqual(self.store.purge(at(20, 12), retention_days=10, forget_random_days=7), (1, 0))
        self.assertEqual(self.readings(), [(SENSOR, at(20, 8), "temperature", 20.0)])
        self.assertEqual(len(self.hourly()), 2)
        self.assertEqual(len(trends.readings(self.store, self.store.find(SENSOR)["id"])), 2)
        self.assertEqual(self.store.purge(at(30, 12), retention_days=0, forget_random_days=7), (0, 0))
        self.assertEqual(len(self.readings()), 1)

    def test_forgetting_a_device_takes_its_readings_along(self):
        # A sensor on a rotating address is forgotten like any other; its
        # readings are not folded into the pseudo-device the way presence is.
        private = Advert(PHONE, -60, "random", service_data=thermometer(11.0).service_data)
        self.store.record_cycle(at(1, 8), [thermometer(10.0), private])
        self.assertEqual(self.store.purge(at(20, 12), retention_days=365, forget_random_days=7), (0, 1))
        self.assertEqual([r[0] for r in self.readings()], [SENSOR])
        self.assertEqual(len(self.hourly()), 1)
        self.store.forget(SENSOR)
        self.assertEqual((self.readings(), self.hourly()), ([], []))

    def test_now_is_the_last_cycle_plus_the_latest_readings(self):
        self.assertEqual(self.store.now(), {"last_cycle": None, "devices": [], "readings": []})
        self.store.record_cycle(at(19, 8), [thermometer(20.0), bthome(SENSOR, "400161"), *adverts((WATCH, -60))])
        self.store.record_cycle(at(19, 8, 1), [thermometer(21.0), *adverts((WATCH, -50), (BEACON, -70))])
        self.store.record_cycle(at(19, 8, 2), adverts((BEACON, -80), (WATCH, -40)))
        now = self.store.now()
        self.assertEqual(now["last_cycle"], at(19, 8, 2))
        self.assertEqual([(d["address"], d["rssi"]) for d in now["devices"]], [(WATCH, -40), (BEACON, -80)])
        self.assertNotIn("id", now["devices"][0])
        # The sensor was not in the last cycle; its readings are, each with its own age.
        self.assertEqual([(r["address"], r["name"], r["kind"], r["value"], r["unit"], r["ts"]) for r in now["readings"]],
                         [(SENSOR, "garden", "battery", 97.0, "%", at(19, 8)),
                          (SENSOR, "garden", "temperature", 21.0, "°C", at(19, 8, 1))])


class ReadingTrendsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        for ts, t in [(at(19, 6), 10.0), (at(19, 6, 30), 12.0), (at(19, 14), 20.0), (at(19, 14, 30), 22.0),
                      (at(20, 6), 11.0), (at(20, 14), 25.0)]:
            self.store.record_cycle(ts, [thermometer(t)])
        self.store.record_cycle(at(19, 6, 10), [bthome(SENSOR, "400161")])
        self.sensor = self.store.find(SENSOR)["id"]

    def summary(self, rows):
        return [(r["when"], r["kind"], r["min"], r["avg"], r["max"]) for r in rows]

    def test_by_day(self):
        self.assertEqual(self.summary(trends.readings(self.store, self.sensor, "temperature")),
                         [("2026-10-19", "temperature", 10.0, 16.0, 22.0), ("2026-10-20", "temperature", 11.0, 18.0, 25.0)])
        rows = trends.readings(self.store, self.sensor)
        self.assertEqual([(r["kind"], r["unit"]) for r in rows], [("battery", "%"), ("temperature", "°C"), ("temperature", "°C")])
        self.assertEqual(trends.readings(self.store, self.sensor, "co2"), [])

    def test_by_hour(self):
        rows = trends.readings(self.store, self.sensor, "temperature", by="hour")
        self.assertEqual([(r["when"], r["avg"]) for r in rows],
                         [("2026-10-19 06", 11.0), ("2026-10-19 14", 21.0), ("2026-10-20 06", 11.0), ("2026-10-20 14", 25.0)])
        with self.assertRaises(ValueError):
            trends.readings(self.store, self.sensor, by="week")

    def test_since_and_until_cut_at_the_hour(self):
        rows = trends.readings(self.store, self.sensor, "temperature", since=at(19, 14, 30), until=at(20, 14), by="hour")
        self.assertEqual([(r["when"], r["min"]) for r in rows], [("2026-10-19 14", 20.0), ("2026-10-20 06", 11.0)])
        self.assertEqual(self.summary(trends.readings(self.store, self.sensor, "temperature", since=at(19, 14, 30))),
                         [("2026-10-19", "temperature", 20.0, 21.0, 22.0), ("2026-10-20", "temperature", 11.0, 18.0, 25.0)])


class OldDatabaseTest(unittest.TestCase):
    """A database written before readings existed: user_version 0 (never set)
    or 1, and no reading tables. The dashboard may be upgraded before the
    daemon is restarted, so a reader must cope until the writer migrates it."""

    def test_reader_finds_no_readings_until_the_writer_migrates(self):
        for version in (0, 1):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "btlog.db"
                with Store(path) as old:
                    old.record_cycle(at(19, 8), adverts((WATCH, -60)))
                    old.conn.executescript(f"DROP TABLE readings; DROP TABLE hourly_readings; "
                                           f"PRAGMA user_version = {version};")
                with Store(path, readonly=True) as r:
                    self.assertFalse(r.has_readings)
                    self.assertEqual(trends.readings(r, r.find(WATCH)["id"]), [])
                    now = r.now()
                    self.assertEqual(([d["address"] for d in now["devices"]], now["readings"]), ([WATCH], []))
                with Store(path) as w:
                    self.assertEqual(w.conn.execute("PRAGMA user_version").fetchone()["user_version"], VERSION)
                    w.record_cycle(at(19, 9), [thermometer(21.0)])
                with Store(path, readonly=True) as r:
                    self.assertTrue(r.has_readings)
                    self.assertEqual([x["value"] for x in r.now()["readings"]], [21.0])
                    self.assertEqual(r.find(WATCH)["cycles"], 1)


if __name__ == "__main__":
    unittest.main()
