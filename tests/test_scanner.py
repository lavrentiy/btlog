import logging
import unittest

from btlog.config import Config
from btlog.scanner import run
from btlog.store import Store
from helpers import PHONE, WATCH, Clock, ScriptedSource, adverts, at


class RunTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.conf = Config(interval=60, retention_days=90, forget_random_days=7)
        logging.getLogger("btlog").setLevel(logging.CRITICAL)

    def test_records_each_cycle_until_the_source_ends(self):
        source = ScriptedSource([adverts((WATCH, -60)), [], adverts((WATCH, -50), (PHONE, -70))])
        clock = Clock(at(19, 8))
        run(source, self.store, self.conf, clock=clock, sleep=clock.sleep)
        self.assertEqual(source.asked, [60, 60, 60, 60])
        s = self.store.status()
        self.assertEqual((s["cycles"], s["devices"], s["private"], s["sightings"]), (3, 1, 1, 3))
        self.assertEqual(self.store.find(WATCH)["cycles"], 2)
        self.assertEqual([x["ts"] for x in self.store.sightings()], [at(19, 8), at(19, 8, 2), at(19, 8, 2)])

    def test_scan_failure_is_retried_not_fatal(self):
        source = ScriptedSource([OSError("adapter gone"), adverts((WATCH, -60))])
        clock = Clock(at(19, 8))
        with self.assertLogs("btlog", level="ERROR") as logs:
            run(source, self.store, self.conf, clock=clock, sleep=clock.sleep, retry=5)
        self.assertIn("adapter gone", logs.output[0])
        self.assertEqual(clock.slept, [5])
        self.assertEqual(self.store.status()["cycles"], 1)

    def test_retention_runs_hourly_from_the_loop(self):
        # A phone seen 30 days ago must be forgotten by the first purge, and the
        # next purge only happens an hour later.
        self.store.record_cycle(at(1, 8), adverts((PHONE, -60)))
        source = ScriptedSource([adverts((WATCH, -60))] * 3)
        clock = Clock(at(31, 8), step=60)
        with self.assertLogs("btlog", level="INFO") as logs:
            run(source, self.store, self.conf, clock=clock, sleep=clock.sleep)
        self.assertEqual(sum("forgot 1 random" in line for line in logs.output), 1)
        self.assertEqual([d["address"] for d in self.store.devices(private=True)], [WATCH])


if __name__ == "__main__":
    unittest.main()
