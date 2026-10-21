"""The scanner: a source of advertisements and the loop that turns them into cycles.

A source is anything with scan(seconds) -> list of Advert: it listens for that
long and returns everything it heard, or None when it has nothing more to give.
The daemon asks for one cycle at a time and records it, so the loop is the same
against BlueZ and against the scripted source the tests use. bleak is imported
only when the real source is built, so nothing else needs it installed.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("btlog")


@dataclass(frozen=True)
class Advert:
    address: str
    rssi: int
    address_type: str | None = None   # "public" or "random" when the backend says
    name: str | None = None
    manufacturer: int | None = None   # Bluetooth SIG company id
    services: tuple = ()


def advert_from(device, adv):
    """An Advert from bleak's (BLEDevice, AdvertisementData), or None.

    On BlueZ bleak also calls back for property changes that are not adverts
    (a paired keyboard connecting, say), and those carry no RSSI; bleak fills
    in -127. Such a report is not a sighting and is dropped. The lowest company
    id is taken when a device carries several, so the vendor does not flip
    from cycle to cycle with dictionary order.
    """
    details = getattr(device, "details", None)
    props = details.get("props", {}) if isinstance(details, dict) else {}
    if adv.rssi is None or (props and "RSSI" not in props):
        return None
    return Advert(
        address=device.address.upper(), rssi=adv.rssi,
        address_type=props.get("AddressType"), name=adv.local_name,
        manufacturer=min(adv.manufacturer_data) if adv.manufacturer_data else None,
        services=tuple(adv.service_uuids))


class BleakSource:
    """Advertisements from the local adapter, through bleak.

    One event loop lives for the whole daemon: bleak keeps its connection to
    BlueZ per loop, so a loop per cycle would reconnect to D-Bus and re-read
    every known device each minute. Discovery itself is started afresh each
    cycle, which has BlueZ report everything in range again.
    """

    def __init__(self, adapter=None):
        self.adapter = adapter
        self.loop = asyncio.new_event_loop()

    def scan(self, seconds):
        return self.loop.run_until_complete(self._scan(seconds))

    async def _scan(self, seconds):
        from bleak import BleakScanner

        heard = []

        def on_advert(device, adv):
            a = advert_from(device, adv)
            if a is not None:
                heard.append(a)

        kwargs = {"adapter": self.adapter} if self.adapter else {}
        async with BleakScanner(on_advert, **kwargs):
            await asyncio.sleep(seconds)
        return heard


def run(source, store, conf, clock=time.time, sleep=time.sleep, retry=None):
    """Scan cycle after cycle until the source returns None.

    A failing scan (adapter unplugged, BlueZ restarting) is logged and retried
    after one interval rather than taking the service down: systemd would restart
    it anyway, and the gap shows in the cycles table either way. Every hour the
    loop logs a one-line summary and applies retention, so there is no second
    timer; single cycles are logged at debug level only, to spare the journal.
    """
    retry = conf.interval if retry is None else retry
    next_hour, cycles, addresses = 0, 0, set()
    while True:
        started = clock()
        if started >= next_hour:
            if next_hour:
                log.info("last hour: %d cycles, %d distinct addresses", cycles, len(addresses))
            sightings, devices = store.purge(started, conf.retention_days, conf.forget_random_days)
            if sightings or devices:
                log.info("purged %d old sightings, forgot %d random addresses", sightings, devices)
            next_hour, cycles, addresses = started + 3600, 0, set()
        try:
            adverts = source.scan(conf.interval)
        except Exception as e:  # noqa: BLE001 - anything from the Bluetooth stack
            log.error("scan failed: %s; retrying in %gs", e, retry)
            sleep(retry)
            continue
        if adverts is None:
            return
        n = store.record_cycle(started, adverts)
        log.debug("cycle: %d adverts, %d devices", len(adverts), n)
        cycles += 1
        addresses.update(a.address for a in adverts)
