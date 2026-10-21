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

    def run_cli(self, *args, db=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(db or self.db), *args])
        return code or 0, out.getvalue(), err.getvalue()

    def other_db(self, *cycles):
        """A database of its own for tests that write, so the shared one keeps its counts."""
        path = Path(self.tmp.name) / f"{self._testMethodName}.db"
        with Store(path) as store:
            for ts, advs in cycles:
                store.record_cycle(ts, advs)
        return path

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
        for args in (["days"], ["show", "garmin"], ["rssi", WATCH, "--by", "hour", "--since", "all"], ["new", "--since", "2026-10-01"], ["top"]):
            code, out, err = self.run_cli(*args)
            self.assertEqual((code, err), (0, ""), args)
            self.assertTrue(out.strip(), args)
        _, out, _ = self.run_cli("show", "garmin")
        self.assertIn("vendor Garmin", out)
        _, out, _ = self.run_cli("--json", "devices")
        self.assertEqual([d["vendor"] for d in json.loads(out)], ["Garmin", ""])

    def test_heat_calendar(self):
        code, out, err = self.run_cli("heat", "garmin", "--since", "all")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[1], "            0  3  6  9  12 15 18 21 ")
        self.assertEqual(lines[2], "2026-10-19        ███" + " " * 15)
        self.assertEqual(lines[3], "2026-10-20  ········█···············")
        code, out, _ = self.run_cli("--json", "heat", "--since", "2026-10-20")
        self.assertEqual(json.loads(out), [{"day": "2026-10-20", "hour": 8, "presence": 2.0}])
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
        self.assertEqual(rows[0], "ts,time,address,kind,name,label,rssi,adverts")
        self.assertEqual(len(rows), 1 + 18 + 1)
        self.assertEqual(rows[-1], f"{int(at(20, 8))},2026-10-20 08:00:00,{WATCH},static,Garmin,,-55,1")
        _, out, _ = self.run_cli("export", "--since", "2026-10-20", "--json")
        self.assertEqual(json.loads(out)[0]["address"], PHONE)

    def test_global_flags_after_the_subcommand(self):
        _, out, _ = self.run_cli("devices", "--json")
        self.assertEqual(json.loads(out)[0]["address"], WATCH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(cli.main(["devices", "--db", str(self.db), "--json"]))
        self.assertEqual(json.loads(out.getvalue())[0]["address"], WATCH)

    def test_since_cuts_at_the_hour(self):
        _, out, _ = self.run_cli("--json", "hours", "garmin", "--since", "2026-10-19T12:00")
        data = json.loads(out)
        self.assertEqual((data[7], data[8], data[12]), (None, 1.0, 0.0))

    def test_forget(self):
        db = self.other_db((at(21, 8), adverts(("DE:AD:BE:EF:00:01", -60, "Neighbour"), (WATCH, -60))))
        code, out, _ = self.run_cli("forget", "neigh", db=db)
        self.assertEqual((code, out), (0, "DE:AD:BE:EF:00:01 Neighbour forgotten, with 1 sightings\n"))
        code, _, err = self.run_cli("show", "neigh", db=db)
        self.assertEqual(code, 1)
        _, out, _ = self.run_cli("--json", "status", db=db)
        self.assertEqual((json.loads(out)["devices"], json.loads(out)["cycles"]), (1, 1))

    def test_names_are_printable(self):
        db = self.other_db((at(21, 9), adverts(("DE:AD:BE:EF:00:02", -60, "evil\x1b[2J\nname"))))
        _, out, _ = self.run_cli("--json", "show", "DE:AD:BE:EF:00:02", db=db)
        self.assertEqual(json.loads(out)["name"], "evil[2Jname")

    def test_status_and_purge(self):
        _, out, _ = self.run_cli("--json", "status")
        s = json.loads(out)
        self.assertEqual((s["cycles"], s["last_cycle"], s["stale"]), (145, at(20, 8), True))
        code, out, _ = self.run_cli("status", "--check")
        self.assertEqual(code, 1)
        self.assertIn("stale: is the daemon running?", out)
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
            p.write_text("retention_days = 0\nforget_random_days = 0\n", encoding="utf-8")
            self.assertEqual((config.load(p).retention_days, config.load(p).forget_random_days), (0, 0))
            for bad in ('interval = 0\n', 'interval = "60"\n', 'retention_days = 1.5\n', 'retention_days = -1\n',
                        'adapter = 1\n', 'forget_random_days = true\n'):
                p.write_text(bad, encoding="utf-8")
                with self.assertRaises(ValueError, msg=bad):
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
        self.assertEqual(when.fmt(now), "2026-10-19 10:30")
        self.assertEqual(when.fmt(None), "-")
        with self.assertRaises(ValueError):
            when.parse("soon", now)


if __name__ == "__main__":
    unittest.main()
