import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from btlog import cli, config, rules, web
from btlog.config import Config
from btlog.store import VERSION, Store
from helpers import BEACON, PHONE, SENSOR, WATCH, adverts, at, bthome, fill_day, thermometer
from test_notify import Recorder

STRANGER = "D0:00:00:00:00:01"   # static random, never labelled
NEWCOMER = "C1:00:00:00:00:01"   # static random, labelled but known for days only


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(map(str, args)))
    return code or 0, out.getvalue(), err.getvalue()


class Scripted(unittest.TestCase):
    """A store fed scripted cycles, and rules.run() at a chosen now."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.conf = Config(interval=60, alerts=["silent", "away"])

    def minutes(self, start, n, *specs, step=60):
        """A cycle every step seconds from start, n of them, hearing the same
        adverts each time; returns the time of the last one."""
        for i in range(n):
            self.store.record_cycle(start + i * step, [*specs])
        return start + (n - 1) * step

    def run_at(self, now):
        return rules.run(self.store, self.conf, now=now)

    def open(self):
        return [(a["rule"], a["name"], a["key"]) for a in rules.listing(self.store)["open"]]

    def closed(self):
        return [(a["rule"], a["name"], a["key"]) for a in rules.listing(self.store)["closed"]]


class AwayTest(Scripted):
    def test_left_after_away_minutes_and_back(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (STRANGER, -70)))
        self.store.set_label(WATCH, "my watch")
        self.minutes(at(19, 8, 1), 16, *adverts((BEACON, -70)))
        self.assertEqual(self.run_at(at(19, 8, 15))["opened"], 0)     # 15 minutes: not yet
        r = self.run_at(at(19, 8, 16))
        self.assertEqual((r["opened"], r["open"], r["sent"], r["errors"]), (1, 1, 0, []))
        # A stranger unheard as long never raises anything: it is not labelled.
        self.assertEqual(self.open(), [("away", "my watch", "")])
        self.assertEqual(rules.listing(self.store)["open"][0]["message"], "my watch left")
        self.assertEqual(self.run_at(at(19, 8, 16))["opened"], 0)     # one episode, not one per run
        self.store.record_cycle(at(19, 8, 17), adverts((WATCH, -60)))
        self.assertEqual(self.run_at(at(19, 8, 17))["closed"], 1)
        self.assertEqual((self.open(), self.closed()), ([], [("away", "my watch", "")]))
        a = rules.listing(self.store)["closed"][0]
        self.assertEqual((a["device"], a["opened"], a["closed"]), (WATCH, at(19, 8, 16), at(19, 8, 17)))

    def test_a_weak_signal_does_not_flap(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -95)))
        self.store.set_label(WATCH, "my watch")
        for minute in range(1, 60):
            heard = adverts((WATCH, -95)) if minute % 10 == 0 else []
            self.store.record_cycle(at(19, 8, minute), heard)
            self.assertEqual(self.run_at(at(19, 8, minute))["opened"], 0, minute)

    def test_forgetting_the_device_or_the_rule_ends_its_episodes(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (BEACON, -60)))
        self.store.set_label(WATCH, "my watch")
        self.store.set_label(BEACON, "door")
        self.minutes(at(19, 8, 1), 20)
        self.run_at(at(19, 8, 20))
        self.assertEqual(len(self.open()), 2)
        self.store.forget(WATCH)
        self.assertEqual(self.open(), [("away", "door", "")])
        self.conf.alerts = ["silent"]
        self.run_at(at(19, 8, 20))
        self.assertEqual((self.open(), self.closed()), ([], []))

    def test_present(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (BEACON, -60), (STRANGER, -60)))
        self.store.record_cycle(at(19, 8, 20), adverts((BEACON, -60), (STRANGER, -60)))
        self.store.set_label(WATCH, "my watch")
        self.store.set_label(BEACON, "door")
        self.assertEqual([d["label"] for d in rules.present(self.store, 15, now=at(19, 8, 21))], ["door"])
        both = rules.present(self.store, 30, now=at(19, 8, 21))
        self.assertEqual([(d["address"], d["last_seen"]) for d in both], [(BEACON, at(19, 8, 20)), (WATCH, at(19, 8))])
        self.assertNotIn("id", both[0])


class SilentTest(Scripted):
    def test_a_labelled_sensor_unheard_for_silent_hours(self):
        self.conf.interval = 600
        other = "A4:C1:38:00:00:02"
        self.store.record_cycle(at(19, 8), [thermometer(-20.0, name=None), thermometer(5.0, other, name=None)])
        self.store.set_label(SENSOR, "freezer")
        last = self.minutes(at(19, 8, 10), 36, step=600)
        self.assertEqual(last, at(19, 14))
        self.assertEqual(self.run_at(at(19, 14))["opened"], 0)        # six hours exactly
        self.minutes(at(19, 14, 10), 1)
        self.run_at(at(19, 14, 10))
        # A sensor is silent, never away; the unlabelled one is nobody's business.
        self.assertEqual(self.open(), [("silent", "freezer", "")])
        self.assertEqual(rules.listing(self.store)["open"][0]["message"], "freezer not heard since 2026-10-19 08:00")
        self.store.record_cycle(at(19, 14, 20), [thermometer(-19.0, name=None)])
        self.run_at(at(19, 14, 20))
        self.assertEqual((self.open(), self.closed()), ([], [("silent", "freezer", "")]))


class RangeTest(Scripted):
    def setUp(self):
        super().setUp()
        self.conf.alerts = ["range"]
        self.conf.range = {"Freezer": {"temperature": [-30, -15], "battery": [20, 100]}}

    def cycle(self, minute, temperature=None, battery=None):
        heard = [thermometer(temperature, name=None)] if temperature is not None else []
        heard += [bthome(SENSOR, f"4001{battery:02x}")] if battery is not None else []
        self.store.record_cycle(at(19, 8, minute), heard)
        return self.run_at(at(19, 8, minute))

    def test_one_alert_per_kind(self):
        self.store.record_cycle(at(19, 7), [thermometer(-20.0, name=None)])
        self.store.set_label(SENSOR, "freezer")
        self.assertEqual(self.cycle(0, -20.0, 97)["opened"], 0)
        self.assertEqual(self.cycle(1, -10.0)["opened"], 1)
        self.assertEqual(rules.listing(self.store)["open"][0]["message"], "freezer temperature -10 °C outside -30 to -15")
        self.cycle(2, -9.5, 10)
        self.assertEqual(self.open(), [("range", "freezer", "battery"), ("range", "freezer", "temperature")])
        self.cycle(3, -18.0)
        self.assertEqual((self.open(), self.closed()), ([("range", "freezer", "battery")], [("range", "freezer", "temperature")]))
        self.assertEqual(self.cycle(4, battery=15)["opened"], 0)       # still out: the same episode
        self.cycle(5, battery=80)
        self.assertEqual(self.open(), [])

    def test_only_recent_readings_count(self):
        self.conf.silent_hours = 1
        self.store.record_cycle(at(19, 6), [thermometer(-5.0, name=None)])
        self.store.set_label(SENSOR, "freezer")
        self.assertEqual(self.cycle(0)["opened"], 0)       # two hours old: silent's business, not range's

    def test_a_label_no_device_has_is_reported(self):
        self.conf.range = {"freezr": {"temperature": [-30, -15]}}
        self.store.record_cycle(at(19, 8), [thermometer(-10.0, name=None)])
        self.store.set_label(SENSOR, "freezer")
        r = self.run_at(at(19, 8))
        self.assertEqual((r["opened"], r["errors"]), (0, ["range: no device labelled 'freezr'"]))


class UnusualTest(Scripted):
    def test_an_hour_the_device_is_never_around(self):
        self.conf.alerts, self.conf.interval = ["unusual"], 1800
        for day in range(1, 31):
            fill_day(self.store, day, {WATCH: set(range(7, 23)), NEWCOMER: set(range(7, 23)) if day >= 28 else set()},
                     interval_minutes=30)
        self.store.set_label(WATCH, "my watch")
        self.store.set_label(NEWCOMER, "new watch")
        self.store.record_cycle(at(31, 3), adverts((WATCH, -60), (NEWCOMER, -60)))
        r = self.run_at(at(31, 3, 10))
        # The newcomer has no habits yet, a week is needed for that.
        self.assertEqual((r["opened"], self.open()), (1, [("unusual", "my watch", "")]))
        self.assertEqual(rules.listing(self.store)["open"][0]["message"],
                         "my watch here at 03:00, around then 0% of the last 30 days")
        self.store.record_cycle(at(31, 3, 30), [])
        self.assertEqual(self.run_at(at(31, 3, 40))["opened"], 0)
        # At eight it is always around: the hour is over and so is the alert.
        self.store.record_cycle(at(31, 8), adverts((WATCH, -60)))
        self.assertEqual(self.run_at(at(31, 8, 5))["closed"], 1)
        self.assertEqual(self.open(), [])

    def test_off_unless_asked_for(self):
        self.assertNotIn("unusual", config.Config().alerts)
        self.assertTrue(all("unusual" not in m["alerts"] for m in config.MODES.values()))


class StaleScannerTest(Scripted):
    def test_absence_is_not_judged_while_the_scanner_is_down(self):
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60), (BEACON, -60)))
        self.store.set_label(WATCH, "my watch")
        self.store.set_label(BEACON, "door")
        self.minutes(at(19, 8, 1), 20, *adverts((BEACON, -60)))
        self.run_at(at(19, 8, 20))
        self.assertEqual(self.open(), [("away", "my watch", "")])
        # No cycle since 8:20. At nine the door is unheard for 40 minutes, but
        # that says nothing; the open episode is neither closed nor repeated.
        r = self.run_at(at(19, 9))
        self.assertEqual((r["opened"], r["closed"]), (1, 0))
        self.assertEqual(self.open(), [("scanner", None, ""), ("away", "my watch", "")])
        self.assertEqual(rules.listing(self.store)["open"][0]["message"], "scanner has not run since 2026-10-19 08:20")
        self.assertEqual(self.run_at(at(19, 9, 30))["opened"], 0)
        # Cycles resume, with the watch and without the door.
        self.store.record_cycle(at(19, 9, 31), adverts((WATCH, -60)))
        r = self.run_at(at(19, 9, 31))
        self.assertEqual((r["opened"], r["closed"]), (1, 2))
        self.assertEqual(self.open(), [("away", "door", "")])

    def test_a_scanner_that_never_ran(self):
        self.conf.alerts = []
        self.assertEqual(self.run_at(at(19, 8))["open"], 1)
        self.assertEqual(rules.listing(self.store)["open"][0]["message"], "scanner has not run yet")
        self.store.record_cycle(at(19, 8, 1), [])
        self.assertEqual(self.run_at(at(19, 8, 1))["closed"], 1)


class DeliveryTest(Scripted):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        super().setUp()
        self.srv.seen, self.srv.status = [], 200
        self.conf.notify = self.url + "/btlog"
        self.store.record_cycle(at(19, 8), adverts((WATCH, -60)))
        self.store.set_label(WATCH, "my watch")
        self.minutes(at(19, 8, 1), 20)

    def bodies(self):
        return [body.decode() for _, _, _, body in self.srv.seen]

    def back(self):
        self.store.record_cycle(at(19, 8, 30), adverts((WATCH, -60)))
        return self.run_at(at(19, 8, 30))

    def test_exactly_one_notification_each_way(self):
        self.assertEqual(self.run_at(at(19, 8, 16))["sent"], 1)
        for minute in (17, 18, 20):
            self.assertEqual(self.run_at(at(19, 8, minute))["sent"], 0)
        self.assertEqual(self.bodies(), ["my watch left"])
        self.assertEqual((self.srv.seen[0][1], self.srv.seen[0][2]["Title"]), ("/btlog", "btlog away"))
        self.assertEqual(rules.listing(self.store)["open"][0]["sent_open"], at(19, 8, 16))
        self.assertEqual(self.back()["sent"], 1)
        self.run_at(at(19, 8, 31))
        self.assertEqual(self.bodies(), ["my watch left", "my watch is back"])
        self.assertEqual(self.srv.seen[1][2]["Tags"], "white_check_mark")
        a = rules.listing(self.store)["closed"][0]
        self.assertEqual((a["sent_open"], a["sent_closed"]), (at(19, 8, 16), at(19, 8, 30)))

    def test_the_alert_as_a_webhook_gets_it(self):
        self.conf.notify = "webhook:" + self.url + "/hook"
        self.run_at(at(19, 8, 16))
        self.assertEqual(json.loads(self.srv.seen[0][3]), {"rule": "away", "state": "open", "device": WATCH,
                                                           "name": "my watch", "message": "my watch left",
                                                           "ts": at(19, 8, 16)})
        self.back()
        self.assertEqual({k: v for k, v in json.loads(self.srv.seen[1][3]).items() if k in ("state", "ts")},
                         {"state": "closed", "ts": at(19, 8, 30)})

    def test_a_failed_delivery_is_retried_next_run(self):
        self.srv.status = 500
        r = self.run_at(at(19, 8, 16))
        self.assertEqual((r["opened"], r["sent"]), (1, 0))
        self.assertRegex(r["errors"][0], r"^notify: http://127\.0\.0\.1:\d+/btlog: HTTP 500 .*; 1 pending, next run tries again$")
        self.assertIsNone(rules.listing(self.store)["open"][0]["sent_open"])
        self.srv.status = 200
        r = self.run_at(at(19, 8, 17))
        self.assertEqual((r["sent"], r["errors"]), (1, []))
        self.run_at(at(19, 8, 18))
        self.assertEqual(self.bodies(), ["my watch left", "my watch left"])   # the 500, then the one delivered

    def test_an_episode_over_before_it_was_delivered_is_not_sent(self):
        self.srv.status = 500
        self.run_at(at(19, 8, 16))
        self.srv.status = 200
        r = self.back()
        self.assertEqual((r["closed"], r["sent"], r["errors"]), (1, 0, []))
        self.assertEqual(len(self.srv.seen), 1)
        a = rules.listing(self.store)["closed"][0]
        self.assertEqual((a["sent_open"], a["sent_closed"]), (None, None))

    def test_without_a_target_the_table_still_fills(self):
        self.conf.notify = None
        r = self.run_at(at(19, 8, 16))
        self.assertEqual((r["opened"], r["sent"], self.srv.seen), (1, 0, []))
        # Set one later and what is still open goes out; nothing older does.
        self.conf.notify = self.url + "/btlog"
        self.assertEqual(self.run_at(at(19, 8, 17))["sent"], 1)


class AlertConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "btlog.toml"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return config.load(self.path)

    def test_defaults_and_keys(self):
        c = config.Config()
        self.assertEqual((c.notify, c.silent_hours, c.away_minutes, c.range), (None, 6, 15, {}))
        c = self.load('notify = "https://ntfy.sh/btlog-7f3a"\nsilent_hours = 2\naway_minutes = 5.5\n'
                      'alerts = ["away", "unusual"]\n[range.freezer]\ntemperature = [-30, -15.5]\nbattery = [20, 100]\n')
        self.assertEqual((c.notify, c.silent_hours, c.away_minutes, c.alerts),
                         ("https://ntfy.sh/btlog-7f3a", 2, 5.5, ["away", "unusual"]))
        self.assertEqual(c.range, {"freezer": {"temperature": [-30, -15.5], "battery": [20, 100]}})
        self.assertEqual(self.load('mode = "sensors"\n').alerts, ["silent", "range"])

    def test_bad_values(self):
        for bad, text in [('alerts = ["silent", "gone"]\n', "unknown rule.*gone"),
                          ('alerts = ["scanner"]\n', "known: silent, away, range, unusual"),
                          ('notify = "ntfy.sh/topic"\n', "not understood"),
                          ('notify = 1\n', "must be a string"),
                          ('notify = "command:"\n', "not understood"),
                          ('silent_hours = 0\n', "positive"),
                          ('away_minutes = "15"\n', "must be a number"),
                          ('range = 1\n', "table per device label"),
                          ('[range]\nfreezer = [-30, -15]\n', "range.freezer must be a table"),
                          ('[range.freezer]\ntemprature = [-30, -15]\n', "unknown kind 'temprature'"),
                          ('[range.freezer]\ntemperature = [-15, -30]\n', "low <= high"),
                          ('[range.freezer]\ntemperature = [-15]\n', r"\[low, high\]"),
                          ('[range.freezer]\ntemperature = ["a", "b"]\n', "two numbers"),
                          ('[range.freezer]\ntemperature = [true, 2]\n', "two numbers"),
                          ('[range.freezer]\ntemperature = -15\n', "two numbers")]:
            with self.assertRaisesRegex(ValueError, text, msg=bad):
                self.load(bad)


class CliTest(unittest.TestCase):
    """`btlog alerts` against a database with real times: the CLI runs at now."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.seen, self.srv.status = [], 200
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db, self.toml = Path(tmp.name) / "btlog.db", Path(tmp.name) / "btlog.toml"
        self.toml.write_text("", encoding="utf-8")
        now = time.time()
        with Store(self.db) as store:
            store.record_cycle(now - 3600, adverts((WATCH, -60), (BEACON, -60)))
            store.record_cycle(now - 30, adverts((BEACON, -60), (PHONE, -60)))
            store.set_label(WATCH, "my watch")

    def cli(self, *args, notify=None):
        self.toml.write_text(f'notify = "{notify}"\n' if notify else "", encoding="utf-8")
        return run_cli("--config", self.toml, "--db", self.db, *args)

    def test_run_then_list(self):
        self.assertEqual(self.cli("alerts"), (0, "no open alerts\n", ""))
        code, out, err = self.cli("alerts", "--run")
        self.assertEqual((code, out, err), (0, "1 opened, 0 closed, 1 open; notifications off (no notify target)\n", ""))
        code, out, _ = self.cli("alerts")
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["OPEN", "SINCE", "RULE", "MESSAGE"])
        self.assertRegex(lines[1], r"^\d{4}-\d\d-\d\d \d\d:\d\d  away  my watch left$")
        _, out, _ = self.cli("--json", "alerts")
        data = json.loads(out)
        self.assertEqual(data["closed"], [])
        self.assertEqual({k: data["open"][0][k] for k in ("rule", "device", "name", "key", "closed", "sent_open")},
                         {"rule": "away", "device": WATCH, "name": "my watch", "key": "", "closed": None, "sent_open": None})
        # A target set later gets what is still open.
        code, out, _ = self.cli("--json", "alerts", "--run", notify=self.url + "/t")
        self.assertEqual(json.loads(out), {"opened": 0, "closed": 0, "open": 1, "sent": 1, "errors": []})
        self.assertEqual(self.srv.seen[0][3], b"my watch left")

    def test_run_with_a_failing_target_still_succeeds(self):
        self.srv.status = 500
        code, out, err = self.cli("alerts", "--run", notify=self.url + "/t")
        self.assertEqual((code, out), (0, "1 opened, 0 closed, 1 open; 0 notifications sent\n"))
        self.assertRegex(err, r"^btlog: notify: http://127\.0\.0\.1:\d+/t: HTTP 500 .*next run tries again\n$")

    def test_test_alert(self):
        code, out, err = self.cli("alerts", "--test", notify=self.url + "/t")
        self.assertEqual((code, out, err), (0, "test alert delivered\n", ""))
        (_, _, headers, body), = self.srv.seen
        self.assertEqual((headers["Title"], body), ("btlog test", b"btlog test alert: notifications work"))
        self.srv.status = 500
        code, out, err = self.cli("alerts", "--test", notify=self.url + "/t")
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"^btlog: http://127\.0\.0\.1:\d+/t: HTTP 500 ")
        code, _, err = self.cli("alerts", "--test")
        self.assertEqual((code, err), (1, "btlog: no notify target: set notify in the config\n"))

    def test_status_check_and_the_rules_agree_on_stale(self):
        self.assertEqual(self.cli("status", "--check")[0], 0)
        self.assertFalse(rules.stale(time.time() - 170, 60, time.time()))
        self.assertTrue(rules.stale(time.time() - 190, 60, time.time()))
        self.assertTrue(rules.stale(None, 60, time.time()))

    def test_reader_on_a_version_3_file_has_no_alerts(self):
        self.cli("alerts", "--run")
        with Store(self.db) as old:
            old.conn.executescript("DROP TABLE alerts; PRAGMA user_version = 3;")
        with Store(self.db, readonly=True) as r:
            self.assertFalse(r.has_alerts)
            self.assertEqual(rules.listing(r), {"open": [], "closed": []})
        self.assertEqual(self.cli("alerts"), (0, "no open alerts\n", ""))
        with Store(self.db) as w:
            self.assertEqual((w.conn.execute("PRAGMA user_version").fetchone()["user_version"], w.has_alerts),
                             (VERSION, True))
        self.assertEqual(self.cli("alerts", "--run")[1], "1 opened, 0 closed, 1 open; notifications off (no notify target)\n")


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "btlog.db"
        now = time.time()
        with Store(db) as store:
            store.record_cycle(now - 3600, adverts((WATCH, -60), (BEACON, -60)))
            store.record_cycle(now - 30, adverts((BEACON, -60), (PHONE, -60)))
            store.set_label(WATCH, "<b>my watch</b>")
            store.set_label(BEACON, "door")
            rules.run(store, Config(), now=now)
        cls.srv = web.make_server(db, "127.0.0.1:0", away_minutes=15)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return json.loads(r.read()) if path.startswith("/api/") else r.read().decode()

    def test_alerts(self):
        data = self.get("/api/alerts")
        self.assertEqual(data["closed"], [])
        self.assertEqual([(a["rule"], a["device"], a["message"]) for a in data["open"]],
                         [("away", WATCH, "<b>my watch</b> left")])

    def test_present(self):
        self.assertEqual([(d["address"], d["label"]) for d in self.get("/api/present")], [(BEACON, "door")])

    def test_page_shows_open_alerts_escaped(self):
        page = self.get("/")
        self.assertIn("/api/alerts", page)
        self.assertIn('id="alertcount"', page)
        self.assertIn("${esc(x.message)}", page)
        self.assertNotIn("my watch", page)


if __name__ == "__main__":
    unittest.main()
