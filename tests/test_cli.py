import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from btlog import cli, config, when
from btlog.store import Store
from helpers import BEACON, PHONE, WATCH, adverts, at, fill_day


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "btlog.db"
        with Store(cls.db) as store:
            fill_day(store, 19, {WATCH: set(range(6, 9)), BEACON: set(range(24))})
            store.record_cycle(at(20, 8), adverts((WATCH, -55, "Garmin", 0x0087), (PHONE, -40)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.db), *args])
        return code or 0, out.getvalue(), err.getvalue()

    def test_devices_table_and_json(self):
        code, out, _ = self.run_cli("devices")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("ADDRESS"))
        self.assertIn("Garmin", out)
        self.assertEqual([l.split()[0] for l in lines[1:3]], [WATCH, BEACON])
        self.assertEqual(lines[3], "(1 rotating private addresses not shown; --private lists them)")
        code, out, _ = self.run_cli("--json", "devices", "--private")
        self.assertEqual([d["address"] for d in json.loads(out)], [PHONE, WATCH, BEACON])
        code, out, _ = self.run_cli("--json", "devices")
        data = json.loads(out)
        self.assertEqual([d["address"] for d in data], [WATCH, BEACON])
        self.assertNotIn("id", data[0])
        self.assertEqual(data[1]["hours"], 24)

    def test_hours_for_all_and_one_device(self):
        code, out, _ = self.run_cli("hours", "--since", "all")
        self.assertEqual(code, 0)
        self.assertIn("average devices", out)
        self.assertRegex(out, r"\n 07  █+\s+2\.0")
        code, out, _ = self.run_cli("--json", "hours", "garmin", "--since", "2026-10-19")
        data = json.loads(out)
        self.assertEqual((data[7], data[12], data[8]), (1.0, 0.0, 1.0))
        _, out, _ = self.run_cli("--json", "hours", "garmin", "--since", "2026-10-20")
        self.assertEqual(json.loads(out)[7:9], [None, 1.0])

    def test_days_show_rssi_new_top(self):
        for args in (["days"], ["show", "garmin"], ["rssi", WATCH, "--by", "hour", "--since", "all"], ["new", "--since", "all"], ["top"]):
            code, out, err = self.run_cli(*args)
            self.assertEqual((code, err), (0, ""), args)
            self.assertTrue(out.strip(), args)
        _, out, _ = self.run_cli("show", "garmin")
        self.assertIn("manufacturer id 135", out)
        _, out, _ = self.run_cli("--json", "new", "--since", "2026-10-20")
        self.assertEqual(json.loads(out), [])
        _, out, _ = self.run_cli("--json", "top", "-n", "1", "--since", "all")
        self.assertEqual(json.loads(out)[0]["address"], BEACON)

    def test_name_then_find_by_label(self):
        code, out, _ = self.run_cli("name", BEACON, "door beacon")
        self.assertEqual(code, 0)
        self.assertIn("door beacon", out)
        _, out, _ = self.run_cli("--json", "show", "door beacon")
        self.assertEqual(json.loads(out)["address"], BEACON)
        self.run_cli("name", BEACON, "")
        code, _, err = self.run_cli("show", "door beacon")
        self.assertEqual(code, 1)
        self.assertIn("no device matching", err)

    def test_export_csv(self):
        _, out, _ = self.run_cli("export", "--since", "all", "-d", "garmin")
        rows = out.splitlines()
        self.assertEqual(rows[0], "time,address,kind,name,label,rssi,adverts")
        self.assertEqual(len(rows), 1 + 18 + 1)
        self.assertTrue(rows[-1].startswith(f"2026-10-20 08:00:00,{WATCH},static,Garmin,,-55,1"))

    def test_status_and_purge(self):
        _, out, _ = self.run_cli("--json", "status")
        self.assertEqual(json.loads(out)["cycles"], 145)
        code, out, _ = self.run_cli("purge")
        self.assertEqual(code, 0)
        self.assertIn("deleted 0 sightings", out)

    def test_errors_are_one_line(self):
        code, _, err = self.run_cli("show", "nobody")
        self.assertEqual((code, err), (1, "btlog: no device matching 'nobody'\n"))
        code, _, err = self.run_cli("hours", "--since", "soon")
        self.assertEqual(code, 1)
        self.assertIn("can't read time", err)
        code, _, err = self.run_cli("--db", str(self.db.parent / "missing.db"), "devices")
        self.assertEqual(code, 1)
        self.assertIn("is the daemon running", err)


class ConfigTest(unittest.TestCase):
    def test_defaults_and_file(self):
        c = config.Config()
        self.assertEqual((c.interval, c.retention_days, c.forget_random_days), (60, 90, 7))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "btlog.toml"
            p.write_text('db = "~/x/btlog.db"\ninterval = 30\nadapter = "hci1"\n', encoding="utf-8")
            c = config.load(p)
            self.assertEqual((c.db, c.interval, c.adapter), (Path("~/x/btlog.db").expanduser(), 30, "hci1"))
            p.write_text("intervall = 30\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "intervall"):
                config.load(p)
            p.write_text("interval = 0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                config.load(p)
            with self.assertRaises(FileNotFoundError):
                config.load(Path(tmp) / "nope.toml")
            os.environ["BTLOG_CONFIG"] = str(Path(tmp) / "nope.toml")
            try:
                with self.assertRaises(FileNotFoundError):
                    config.load()
            finally:
                del os.environ["BTLOG_CONFIG"]


class WhenTest(unittest.TestCase):
    def test_parse(self):
        now = at(19, 10, 30)
        self.assertEqual(when.parse("2h", now), now - 7200)
        self.assertEqual(when.parse("today", now), at(19, 0))
        self.assertEqual(when.parse("yesterday", now), at(18, 0))
        self.assertEqual(when.parse("2026-10-18T19:30", now), at(18, 19, 30))
        self.assertIsNone(when.parse("all", now))
        self.assertEqual(when.day(now), "2026-10-19")
        self.assertEqual(when.day(None), "")
        self.assertEqual(when.fmt(now), "2026-10-19 10:30")
        with self.assertRaises(ValueError):
            when.parse("soon", now)


if __name__ == "__main__":
    unittest.main()
