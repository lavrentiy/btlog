import asyncio
import logging
import sys
import types
import unittest
from types import SimpleNamespace as NS

from btlog.config import Config
from btlog.scanner import Advert, BleakSource, advert_from, run
from btlog.store import Store
from helpers import BEACON, BTHOME, PHONE, SENSOR, WATCH, Clock, ScriptedSource, adverts, at, thermometer


class FakeScanner:
    """Stands in for bleak.BleakScanner: calls back once on entry, and can hang
    on exit the way a wedged bluetoothd would."""
    kwargs = None
    hang = 0

    def __init__(self, callback, **kwargs):
        self.callback = callback
        FakeScanner.kwargs = kwargs

    async def __aenter__(self):
        dev = NS(address="c0:aa:bb:cc:dd:ee", details={"path": "x", "props": {"AddressType": "random", "RSSI": -55}})
        self.callback(dev, NS(rssi=-55, local_name="Watch", manufacturer_data={}, service_data={}, service_uuids=[]))
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(FakeScanner.hang)


class BleakSourceTest(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("bleak")
        fake.BleakScanner = FakeScanner
        self.addCleanup(sys.modules.pop, "bleak", None)
        sys.modules["bleak"] = fake
        FakeScanner.hang = 0

    def test_scan_collects_adverts_on_one_loop(self):
        source = BleakSource("hci1")
        self.addCleanup(source.loop.close)
        first, second = source.scan(0.01), source.scan(0.01)
        self.assertEqual([a.address for a in first], ["C0:AA:BB:CC:DD:EE"])
        self.assertEqual(first, second)
        self.assertEqual(FakeScanner.kwargs, {"adapter": "hci1"})

    def test_hung_scan_times_out_and_is_retried(self):
        FakeScanner.hang = 5
        source = BleakSource(grace=0.05)
        self.addCleanup(source.loop.close)
        with self.assertRaises(TimeoutError):
            source.scan(0.01)


class AdvertFromTest(unittest.TestCase):
    """What bleak hands over, as fakes: BLEDevice(address, details) and
    AdvertisementData(rssi, local_name, manufacturer_data, service_data, service_uuids)."""

    def adv(self, rssi=-60, name="Watch", mfr=None, uuids=(), service=None):
        return NS(rssi=rssi, local_name=name, manufacturer_data=mfr or {}, service_data=service or {},
                  service_uuids=list(uuids))

    def test_bluez_device(self):
        dev = NS(address="c0:aa:bb:cc:dd:ee", details={"path": "/org/bluez/hci0/dev_C0", "props": {"AddressType": "random", "RSSI": -60}})
        a = advert_from(dev, self.adv(mfr={0x004C: b"\x01", 0x0006: b"\x02"}, uuids=["180d"]))
        self.assertEqual((a.address, a.rssi, a.address_type, a.name, a.manufacturer),
                         ("C0:AA:BB:CC:DD:EE", -60, "random", "Watch", 0x0006))

    def test_payloads_are_kept_as_bytes(self):
        bthome = "0000fcd2-0000-1000-8000-00805f9b34fb"
        dev = NS(address="a4:c1:38:00:00:01", details={"path": "x", "props": {"AddressType": "public", "RSSI": -70}})
        a = advert_from(dev, self.adv(mfr={0x0499: bytearray(b"\x05\x12")}, service={bthome: bytearray(b"\x40\x01\x61")}))
        self.assertEqual(a.manufacturer_data, {0x0499: b"\x05\x12"})
        self.assertEqual(a.service_data, {bthome: b"\x40\x01\x61"})
        self.assertIs(type(a.service_data[bthome]), bytes)
        self.assertEqual(a.manufacturer, 0x0499)
        empty = advert_from(dev, self.adv())
        self.assertEqual((empty.manufacturer_data, empty.service_data), ({}, {}))

    def test_other_backend_has_no_address_type(self):
        a = advert_from(NS(address="C0:AA:BB:CC:DD:EE", details=object()), self.adv(name=None))
        self.assertEqual((a.address_type, a.name, a.manufacturer), (None, None, None))

    def test_property_change_without_rssi_is_not_a_sighting(self):
        dev = NS(address="C0:AA:BB:CC:DD:EE", details={"path": "x", "props": {"Connected": True}})
        self.assertIsNone(advert_from(dev, self.adv(rssi=-127)))
        self.assertIsNone(advert_from(NS(address="C0:AA:BB:CC:DD:EE", details=None), self.adv(rssi=None)))


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

    def test_keep_and_payloads_come_from_the_config(self):
        tag = Advert(BEACON, -60, "public", manufacturer_data={0x004C: b"\x02"})
        source = ScriptedSource([[*adverts((WATCH, -60), (PHONE, -70)), tag, thermometer(20.0)]])
        clock = Clock(at(19, 8))
        run(source, self.store, Config(keep="sensors", payloads=True), clock=clock, sleep=clock.sleep)
        self.assertEqual([d["address"] for d in self.store.devices(private=True)], [SENSOR])
        self.assertEqual([r["payloads"] for r in self.store.sightings(payloads=True)], [{"s:" + BTHOME: "4002d007"}])

    def test_scan_failure_is_retried_not_fatal(self):
        source = ScriptedSource([OSError("adapter gone"), adverts((WATCH, -60))])
        clock = Clock(at(19, 8))
        with self.assertLogs("btlog", level="ERROR") as logs:
            run(source, self.store, self.conf, clock=clock, sleep=clock.sleep, retry=5)
        self.assertIn("adapter gone", logs.output[0])
        self.assertEqual(clock.slept, [5])
        self.assertEqual(self.store.status()["cycles"], 1)

    def test_retention_and_summary_run_hourly_from_the_loop(self):
        # A phone seen 30 days ago is forgotten by the first purge; the next
        # purge and the hourly summary come once the clock has passed an hour.
        self.store.record_cycle(at(1, 8), adverts((PHONE, -60)))
        source = ScriptedSource([adverts((WATCH, -60))] * 4)
        clock = Clock(at(31, 8), step=1300)
        with self.assertLogs("btlog", level="INFO") as logs:
            run(source, self.store, self.conf, clock=clock, sleep=clock.sleep)
        self.assertEqual(sum("forgot 1 random" in line for line in logs.output), 1)
        self.assertEqual([line for line in logs.output if "last hour" in line],
                         ["INFO:btlog:last hour: 3 cycles, 1 distinct addresses"])
        self.assertEqual([d["address"] for d in self.store.devices(private=True)], [WATCH])


if __name__ == "__main__":
    unittest.main()
