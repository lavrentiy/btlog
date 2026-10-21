"""Fill a database with ten days of plausible sightings, to try the CLI and the
dashboard without a Bluetooth adapter: a watch that leaves for work on weekdays,
a beacon that is always there, a TV in the evenings, phones with rotating
addresses, a garden thermometer (BTHome, warmest mid-afternoon), a new device
on the last day, and a Wednesday the scanner was off.

    PYTHONPATH=src python tools/demo.py demo.db && PYTHONPATH=src python -m btlog --db demo.db web
"""
import math
import random
import struct
import sys
from datetime import datetime, timedelta

from btlog.decoders import SIG_BASE
from btlog.scanner import Advert
from btlog.store import Store

random.seed(1)


def thermometer(t):
    """BTHome v2 service data as a Shelly BLU H&T sends it: battery, temperature
    in 0.01 °C, humidity in 0.01 %; the night is colder and damper, and each
    day a little warmer than the last."""
    temp = 9 + 0.3 * (t - start).days + 5 * math.sin(2 * math.pi * (t.hour + t.minute / 60 - 9) / 24) + random.gauss(0, 0.2)
    data = struct.pack("<BBBBhBH", 0x40, 0x01, 94, 0x02, round(temp * 100), 0x03, round((80 - 2.5 * (temp - 9)) * 100))
    return Advert("38:39:8F:00:00:01", random.randint(-90, -80), "public", "SBHT-003C",
                  service_data={"0000fcd2" + SIG_BASE: data})


db = sys.argv[1]
start = datetime(2026, 10, 11)
with Store(db) as store:
    t = start
    rpa = f"4A:{random.randrange(256):02X}:00:00:00:01"
    while t < datetime(2026, 10, 21, 12):
        adverts = [Advert("00:11:22:33:44:55", random.randint(-75, -60), "public", "door-beacon", 0x004C)]
        h, wd = t.hour, t.weekday()
        home = wd >= 5 or h < 8 or h >= 18
        if home and random.random() < 0.95:
            adverts.append(Advert("C0:AA:BB:CC:DD:EE", random.randint(-70, -45), "random", "Garmin Forerunner", 0x0087))
        if random.random() < 0.9:  # out in the garden, so not heard every cycle
            adverts.append(thermometer(t))
        if 19 <= h <= 23 and random.random() < 0.8:
            adverts.append(Advert("D4:12:34:56:78:9A", random.randint(-80, -55), "public", "[TV] Living room", 0x0075))
        if t.minute % 15 == 0:
            rpa = f"4A:{random.randrange(256):02X}:{random.randrange(256):02X}:00:00:01"
        if home and random.random() < 0.7:
            adverts.append(Advert(rpa, random.randint(-65, -40), "random"))
        if t >= datetime(2026, 10, 21, 9):
            adverts.append(Advert("F0:99:88:77:66:55", random.randint(-60, -50), "random", "JBL Flip 6"))
        if not (wd == 2 and 10 <= h < 14):  # Wednesday morning the scanner was off
            store.record_cycle(t.timestamp(), [a for a in adverts for _ in range(random.randint(1, 4))])
        t += timedelta(minutes=1)
    print(store.purge(datetime(2026, 10, 21, 12).timestamp(), 90, 7))
    store.set_label("C0:AA:BB:CC:DD:EE", "my watch")
    store.set_label("38:39:8F:00:00:01", "garden")
    print(store.status())
