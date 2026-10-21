import unittest

from btlog import trends, vendors
from btlog.store import Store
from helpers import BEACON, PHONE, WATCH, adverts, at, days_in_week, fill_day


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        # Day 19: the watch is there in the morning and evening, the beacon all
        # day, the phone only during half of hour 12 (3 of 6 cycles).
        per_hour = fill_day(self.store, 19, {WATCH: set(range(6, 9)) | set(range(18, 24)), BEACON: set(range(24))})
        for minute in (0, 10, 20):
            self.store.record_cycle(at(19, 12, minute), adverts((PHONE, -60)))
        self.assertEqual(per_hour, 6)
        self.watch = self.store.find(WATCH)["id"]
        self.phone = self.store.find(PHONE)["id"]

    def test_hours_for_one_device_are_fractions_of_cycles(self):
        h = trends.hours(self.store, self.watch)
        self.assertEqual(h[7], 1.0)
        self.assertEqual(h[12], 0.0)
        self.assertEqual(trends.hours(self.store, self.phone)[12], 3 / 9)  # 6 cycles + 3 extra at 12

    def test_hours_for_all_devices_count_devices_per_cycle(self):
        h = trends.hours(self.store)
        self.assertEqual(h[7], 2.0)                      # beacon + watch
        self.assertEqual(h[13], 1.0)                     # beacon alone
        self.assertAlmostEqual(h[12], (6 + 3) / 9)       # beacon in 6 of the 9 cycles, phone in 3

    def test_slots_the_scanner_never_covered_are_none(self):
        empty = Store(":memory:")
        self.addCleanup(empty.close)
        empty.record_cycle(at(19, 8), adverts((WATCH, -60)))
        h = trends.hours(empty)
        self.assertEqual(h[8], 1.0)
        self.assertEqual(h[9], None)
        self.assertEqual(h.count(None), 23)

    def test_since_limits_the_days(self):
        fill_day(self.store, 20, {WATCH: set(range(24))})
        self.assertEqual(trends.hours(self.store, self.watch)[13], 0.5)
        self.assertEqual(trends.hours(self.store, self.watch, since=at(20, 0))[13], 1.0)

    def test_since_cuts_at_the_hour_not_the_day(self):
        h = trends.hours(self.store, self.watch, since=at(19, 12, 30))
        self.assertEqual((h[7], h[11], h[12], h[18]), (None, None, 0.0, 1.0))
        self.assertEqual(len(trends.timeline(self.store, self.watch, since=at(19, 12, 30))), 12)
        self.assertEqual(trends.rssi(self.store, self.watch, since=at(19, 18)), [{"when": "2026-10-19", "max": -60, "avg": -60.0}])
        top = trends.busiest(self.store, since=at(19, 9))
        self.assertEqual([(d["address"], d["cycles_heard"]) for d in top], [(BEACON, 90), (WATCH, 36)])
        self.assertEqual(trends.busiest(self.store)[1]["cycles_heard"], 54)

    def test_timeline_includes_absent_hours(self):
        t = trends.timeline(self.store, self.watch, since=at(19, 0))
        self.assertEqual(len(t), 24)
        self.assertEqual([r["presence"] for r in t[5:9]], [0.0, 1.0, 1.0, 1.0])
        everyone = trends.timeline(self.store, since=at(19, 0))
        self.assertEqual([r["presence"] for r in everyone[5:9]], [1.0, 2.0, 2.0, 2.0])
        self.assertEqual(everyone[12]["presence"], 1.0)


class WeekdayTest(unittest.TestCase):
    def test_weekday_presence(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        mondays, tuesdays = days_in_week(0)[:2], days_in_week(1)[:2]
        for d in mondays:
            fill_day(store, d, {WATCH: set(range(24))}, interval_minutes=30)
        for d in tuesdays:
            fill_day(store, d, {WATCH: set(range(12))}, interval_minutes=30)
        w = trends.weekdays(store, store.find(WATCH)["id"])
        self.assertEqual(w[0], 1.0)
        self.assertEqual(w[1], 0.5)
        self.assertEqual(w[2:], [None] * 5)


class VendorTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(vendors.name(0x004C), "Apple")
        self.assertEqual(vendors.name(0x1234), "0x1234")
        self.assertEqual(vendors.name(None), "")


class RssiAndNewTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.record_cycle(at(19, 8, 0), adverts((WATCH, -60)))
        self.store.record_cycle(at(19, 8, 10), adverts((WATCH, -70)))
        self.store.record_cycle(at(19, 9, 0), adverts((WATCH, -50)))
        self.store.record_cycle(at(20, 9, 0), adverts((WATCH, -80), (BEACON, -90), (PHONE, -40)))
        self.watch = self.store.find(WATCH)["id"]

    def test_rssi_by_day_and_hour(self):
        by_day = trends.rssi(self.store, self.watch)
        self.assertEqual([(r["when"], r["max"], r["avg"]) for r in by_day],
                         [("2026-10-19", -50, -60.0), ("2026-10-20", -80, -80.0)])
        by_hour = trends.rssi(self.store, self.watch, since=at(19, 0), by="hour")
        self.assertEqual([(r["when"], r["max"], r["avg"]) for r in by_hour],
                         [("2026-10-19 08", -60, -65.0), ("2026-10-19 09", -50, -50.0), ("2026-10-20 09", -80, -80.0)])
        with self.assertRaises(ValueError):
            trends.rssi(self.store, self.watch, by="week")

    def test_new_devices_skip_rotating_addresses(self):
        self.assertEqual([d["address"] for d in trends.new(self.store, at(20, 0))], [BEACON])
        self.assertEqual([d["address"] for d in trends.new(self.store, at(19, 0))], [BEACON, WATCH])

    def test_busiest(self):
        top = trends.busiest(self.store, limit=5)
        self.assertEqual([(d["address"], d["hours"], d["cycles_heard"]) for d in top], [(WATCH, 3, 4), (BEACON, 1, 1)])


if __name__ == "__main__":
    unittest.main()
