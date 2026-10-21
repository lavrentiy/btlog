import random
import unittest

from btlog.decoders import SIG_BASE, UNITS, decode

BTHOME = "0000fcd2" + SIG_BASE
ENV = "0000181a" + SIG_BASE
RUUVI = 0x0499


def bthome(hexstr):
    return decode({}, {BTHOME: bytes.fromhex(hexstr)})


class Readings(unittest.TestCase):
    def assertReadings(self, got, expected):
        self.assertEqual(set(got), set(expected))
        for kind, value in expected.items():
            self.assertIsInstance(got[kind], float)
            self.assertAlmostEqual(got[kind], value, places=6, msg=kind)


class BTHomeTest(Readings):
    def test_spec_example_advertisement(self):
        self.assertReadings(bthome("4002C40903BF13"), {"temperature": 25.0, "humidity": 50.55})

    def test_spec_example_of_each_object(self):
        for obj, kind, value in [
                ("0161", "battery", 97), ("02CA09", "temperature", 25.06), ("451101", "temperature", 27.3),
                ("03BF13", "humidity", 50.55), ("2E23", "humidity", 35), ("04138A01", "pressure", 1008.83),
                ("05138A14", "illuminance", 13460.67), ("0C020C", "voltage", 3.074), ("4A020C", "voltage", 307.4),
                ("14020C", "moisture", 30.74), ("2F23", "moisture", 35), ("12E204", "co2", 1250)]:
            with self.subTest(obj):
                self.assertReadings(bthome("40" + obj), {kind: value})

    def test_packet_id_and_binary_sensors_are_skipped(self):
        # packet id 9, battery, window open, temperature in 0.1 °C, button press
        got = bthome("40" "0009" "0161" "2D01" "451101" "3A01")
        self.assertReadings(got, {"battery": 97, "temperature": 27.3})

    def test_temperature_is_signed(self):
        self.assertReadings(bthome("400218FC"), {"temperature": -10.0})

    def test_first_of_a_repeated_kind_wins(self):
        self.assertReadings(bthome("4002C409451101"), {"temperature": 25.0})

    def test_trigger_flag_still_decodes(self):
        self.assertReadings(bthome("440161"), {"battery": 97})

    def test_encrypted_or_other_versions_give_nothing(self):
        self.assertEqual(bthome("4102C40903BF13"), {})       # encryption bit
        self.assertEqual(bthome("2002C40903BF13"), {})       # version 1
        self.assertEqual(bthome("6002C40903BF13"), {})       # version 3
        self.assertEqual(bthome(""), {})

    def test_unknown_object_ends_parsing_but_keeps_what_came_before(self):
        self.assertReadings(bthome("40" "0161" "02C409" "7F0102" "03BF13"), {"battery": 97, "temperature": 25.0})

    def test_truncated_object_is_dropped(self):
        self.assertEqual(bthome("4002C4"), {})
        self.assertReadings(bthome("40016103BF"), {"battery": 97})


class RuuviTest(Readings):
    # The test vectors of https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
    def ruuvi(self, hexstr):
        return decode({RUUVI: bytes.fromhex(hexstr)}, {})

    def test_valid_data(self):
        self.assertReadings(self.ruuvi("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F"),
                            {"temperature": 24.3, "humidity": 53.49, "pressure": 1000.44, "voltage": 2.977})

    def test_maximum_values(self):
        self.assertReadings(self.ruuvi("057FFFFFFEFFFE7FFF7FFF7FFFFFDEFEFFFECBB8334C884F"),
                            {"temperature": 163.835, "humidity": 163.835, "pressure": 1155.34, "voltage": 3.646})

    def test_minimum_values(self):
        self.assertReadings(self.ruuvi("058001000000008001800180010000000000CBB8334C884F"),
                            {"temperature": -163.835, "humidity": 0.0, "pressure": 500.0, "voltage": 1.6})

    def test_invalid_values_are_not_readings(self):
        self.assertEqual(self.ruuvi("058000FFFFFFFF800080008000FFFFFFFFFFFFFFFFFFFFFF"), {})

    def test_one_missing_value_keeps_the_others(self):
        got = self.ruuvi("058000" "5394C37C0004FFFC040CAC364200CDCBB8334C884F")
        self.assertReadings(got, {"humidity": 53.49, "pressure": 1000.44, "voltage": 2.977})

    def test_truncated_or_other_formats_give_nothing(self):
        valid = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")
        self.assertEqual(decode({RUUVI: valid[:23]}, {}), {})
        self.assertEqual(decode({RUUVI: b"\x03" + valid[1:]}, {}), {})


class AtcTest(Readings):
    def atc(self, hexstr):
        return decode({}, {ENV: bytes.fromhex(hexstr)})

    def test_pvvx_custom_format(self):
        # MAC lowest byte first, 22.79 °C, 49.40 %, 2990 mV, 85 %, counter, flags
        self.assertReadings(self.atc("ddccbb38c1a4" "e708" "4c13" "ae0b" "55" "1c" "04"),
                            {"temperature": 22.79, "humidity": 49.4, "voltage": 2.99, "battery": 85})
        self.assertReadings(self.atc("ddccbb38c1a4" "18fc" "4c13" "ae0b" "55" "1c" "04"),
                            {"temperature": -10.0, "humidity": 49.4, "voltage": 2.99, "battery": 85})

    def test_atc1441_format(self):
        # MAC in order, 22.7 °C, 49 %, 85 %, 2990 mV big endian, counter
        self.assertReadings(self.atc("a4c138bbccdd" "00e3" "31" "55" "0bae" "07"),
                            {"temperature": 22.7, "humidity": 49, "battery": 85, "voltage": 2.99})

    def test_atc1441_readme_placeholder_example(self):
        # The README's example has one placeholder byte value per field, which
        # pins the positions; its battery of 0xcc (204 %) is no reading at all.
        sd = bytes([0x0e, 0x16, 0x1a, 0x18, 0, 0, 0, 0, 0, 0, 0xaa, 0xaa, 0xbb, 0xcc, 0xdd, 0xdd, 0x00])[4:]
        self.assertReadings(decode({}, {ENV: sd}), {"temperature": -2184.6, "humidity": 187, "voltage": 56.797})

    def test_encrypted_and_truncated_give_nothing(self):
        self.assertEqual(self.atc("00" * 11), {})    # pvvx encrypted
        self.assertEqual(self.atc("00" * 8), {})     # atc1441 encrypted
        self.assertEqual(self.atc("ddccbb38c1a4e7084c13ae0b551c"), {})


class HostileTest(unittest.TestCase):
    def test_unknown_sources_give_nothing(self):
        self.assertEqual(decode({0x004C: bytes.fromhex("0215" + "00" * 21)}, {}), {})
        self.assertEqual(decode({}, {"0000fe95" + SIG_BASE: b"\x50\x20\xaa\x01"}), {})
        self.assertEqual(decode({}, {}), {})

    def test_random_bytes_never_raise(self):
        rng = random.Random(1441)
        for n in range(40):
            for _ in range(25):
                junk = rng.randbytes(n)
                for payload in (junk, b"\x40" + junk, b"\x05" + junk):
                    got = decode({RUUVI: payload, 0x004C: payload}, {BTHOME: payload, ENV: payload, "fcd2": payload})
                    self.assertTrue(set(got) <= set(UNITS), got)
                    self.assertTrue(all(isinstance(v, float) for v in got.values()), got)
                    self.assertTrue(0 <= got.get("battery", 0) <= 100)


if __name__ == "__main__":
    unittest.main()
