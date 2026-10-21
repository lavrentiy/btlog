"""Readings carried in advertisements: temperature, humidity, battery and the like.

Many sensors put their readings straight into the advertisement, so hearing
them is enough: no connection, no pairing, and btlog hears them every cycle
anyway. Each format is one small function from payload bytes to {kind: value},
registered under the company id or service UUID it arrives on. The bytes come
from any stranger in range, so a decoder that does not recognise them returns
{} instead of raising, and an unreadable value is dropped without losing the
others. Victron Instant Readout and encrypted BTHome are left out on purpose:
both need AES, which the standard library lacks.
"""

import struct

UNITS = {"temperature": "°C", "humidity": "%", "pressure": "hPa", "battery": "%",
         "voltage": "V", "illuminance": "lx", "moisture": "%", "co2": "ppm"}

SIG_BASE = "-0000-1000-8000-00805f9b34fb"   # a 16-bit UUID xxxx is 0000xxxx + this

# BTHome v2, https://bthome.io/format/ : object id -> (kind, length, signed,
# divisor), little endian. Kind None is an object that is skipped but whose
# length is known, so the objects after it can still be read. When a kind
# comes twice (temperature in 0.01 and in 0.1), the first one is kept.
_BTHOME = {
    0x00: (None, 1, False, 1),              # packet id
    0x01: ("battery", 1, False, 1),
    0x02: ("temperature", 2, True, 100),
    0x03: ("humidity", 2, False, 100),
    0x04: ("pressure", 3, False, 100),
    0x05: ("illuminance", 3, False, 100),
    0x0C: ("voltage", 2, False, 1000),
    0x12: ("co2", 2, False, 1),
    0x14: ("moisture", 2, False, 100),
    0x2E: ("humidity", 1, False, 1),
    0x2F: ("moisture", 1, False, 1),
    0x3A: (None, 1, False, 1),              # button event
    0x3C: (None, 2, False, 1),              # dimmer event
    0x45: ("temperature", 2, True, 10),
    0x4A: ("voltage", 2, False, 10),
}
_BTHOME.update({i: (None, 1, False, 1) for i in (*range(0x0F, 0x12), *range(0x15, 0x2E))})  # binary sensors


def bthome(data):
    """BTHome v2 service data (UUID 0xFCD2), unencrypted.

    Objects have no length field, so an id missing from the table ends the
    parse: everything after it would be read at the wrong offset. The spec
    has senders put ids in ascending order for exactly this reason, so the
    common readings come before anything newer than this table.
    """
    if not data or data[0] & 0x01 or data[0] >> 5 != 2:     # encrypted, or not version 2
        return {}
    out, i = {}, 1
    while i < len(data) and data[i] in _BTHOME:
        kind, size, signed, divisor = _BTHOME[data[i]]
        raw = data[i + 1:i + 1 + size]
        if len(raw) < size:
            break
        if kind:
            out.setdefault(kind, int.from_bytes(raw, "little", signed=signed) / divisor)
        i += 1 + size
    return out


def ruuvi(data):
    """Ruuvi RAWv2, data format 5 (manufacturer 0x0499), big endian.

    https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
    Each field has an all-ones (or 0x8000) value meaning the sensor has no
    reading, which must not be logged as -163.8 °C or 655 % humidity.
    """
    if len(data) != 24 or data[0] != 5:
        return {}
    t, h, p, power = struct.unpack_from(">hHH6xH", data, 1)    # 6x: acceleration
    out = {}
    if t != -0x8000:
        out["temperature"] = t / 200                            # 0.005 °C
    if h != 0xFFFF:
        out["humidity"] = h / 400                               # 0.0025 %
    if p != 0xFFFF:
        out["pressure"] = (p + 50000) / 100                     # Pa above 50000, to hPa
    if power >> 5 != 0x7FF:
        out["voltage"] = ((power >> 5) + 1600) / 1000           # mV above 1600, top 11 bits
    return out


def atc(data):
    """Xiaomi LYWSD03MMC and kin on pvvx or atc1441 custom firmware (UUID 0x181A).

    https://github.com/pvvx/ATC_MiThermometer#custom-format-all-data-little-endian
    https://github.com/atc1441/ATC_MiThermometer#advertising-format-of-the-custom-firmware
    Both start with the MAC and share the UUID, so only the length tells them
    apart: 15 bytes is pvvx (little endian, 0.01 units), 13 is atc1441 (big
    endian, per its firmware's ble.c; 0.1 °C and whole percent). Their
    encrypted variants are 11 and 8 bytes and fall through to {}. The original
    atc1441 firmware sends °F when its display is set to °F, which the bytes
    do not reveal.
    """
    if len(data) == 15:
        t, h, mv, battery = struct.unpack_from("<hHHB", data, 6)
        return {"temperature": t / 100, "humidity": h / 100, "voltage": mv / 1000, "battery": float(battery)}
    if len(data) == 13:
        t, h, battery, mv = struct.unpack_from(">hBBH", data, 6)
        return {"temperature": t / 10, "humidity": float(h), "voltage": mv / 1000, "battery": float(battery)}
    return {}


MANUFACTURERS = {0x0499: ruuvi}
SERVICES = {"0000fcd2" + SIG_BASE: bthome, "0000181a" + SIG_BASE: atc}


def full_uuid(uuid):
    """A service UUID in bleak's 128-bit lower-case spelling: a 16- or 32-bit
    short one is put onto the Bluetooth base UUID, so one service has one name."""
    uuid = uuid.lower()
    return f"{uuid:0>8}{SIG_BASE}" if len(uuid) in (4, 8) else uuid


def decode(manufacturer_data, service_data):
    """All readings found in one advertisement, {kind: value}, {} if none.

    A battery level outside 0-100 % is dropped: no format defines one, so it
    can only be a stranger's bytes that happened to fit a known layout.
    """
    out = {}
    for company, data in manufacturer_data.items():
        if company in MANUFACTURERS:
            out.update(MANUFACTURERS[company](bytes(data)))
    for uuid, data in service_data.items():
        decoder = SERVICES.get(full_uuid(uuid))
        if decoder:
            out.update(decoder(bytes(data)))
    if not 0 <= out.get("battery", 0) <= 100:
        del out["battery"]
    return out
