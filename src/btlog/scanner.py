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


class BleakSource:
    """Advertisements from the local adapter, through bleak.

    Discovery is started afresh every cycle rather than left running: BlueZ only
    reports a device again when something about it changes, so a device that
    just sits there would otherwise be heard once and never again. Stopping and
    starting discovery makes BlueZ announce everything in range each cycle.
    """

    def __init__(self, adapter=None):
        self.adapter = adapter

    def scan(self, seconds):
        return asyncio.run(self._scan(seconds))

    async def _scan(self, seconds):
        from bleak import BleakScanner

        heard = []

        def on_advert(device, adv):
            props = getattr(device, "details", None)
            props = props.get("props", {}) if isinstance(props, dict) else {}
            heard.append(Advert(
                address=device.address.upper(), rssi=adv.rssi,
                address_type=props.get("AddressType"), name=adv.local_name,
                manufacturer=next(iter(adv.manufacturer_data), None),
                services=tuple(adv.service_uuids)))

        kwargs = {"adapter": self.adapter} if self.adapter else {}
        async with BleakScanner(on_advert, **kwargs):
            await asyncio.sleep(seconds)
        return heard


def run(source, store, conf, clock=time.time, sleep=time.sleep, retry=None):
    """Scan cycle after cycle until the source returns None.

    A failing scan (adapter unplugged, BlueZ restarting) is logged and retried
    after one interval rather than taking the service down: systemd would restart
    it anyway, and the gap shows in the cycles table either way. Retention is
    applied once an hour from inside the loop, so there is no second timer.
    """
    retry = conf.interval if retry is None else retry
    next_purge = 0
    while True:
        started = clock()
        try:
            adverts = source.scan(conf.interval)
        except Exception as e:  # noqa: BLE001 - anything from the Bluetooth stack
            log.error("scan failed: %s; retrying in %gs", e, retry)
            sleep(retry)
            continue
        if adverts is None:
            return
        n = store.record_cycle(started, adverts)
        log.info("cycle: %d adverts, %d devices", len(adverts), n)
        if started >= next_purge:
            sightings, devices = store.purge(started, conf.retention_days, conf.forget_random_days)
            if sightings or devices:
                log.info("purged %d old sightings, forgot %d random addresses", sightings, devices)
            next_purge = started + 3600
