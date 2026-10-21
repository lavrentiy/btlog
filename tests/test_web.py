import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from btlog import web
from btlog.store import Store
from helpers import BEACON, WATCH, adverts, at, fill_day


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "btlog.db"
        with Store(db) as store:
            fill_day(store, 19, {WATCH: set(range(6, 9)), BEACON: set(range(24))})
            store.record_cycle(at(20, 8), adverts((WATCH, -55, "Garmin")))
            store.set_label(BEACON, "door")
        cls.srv = web.make_server(db, "127.0.0.1:0")
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            body = r.read().decode()
            return r.status, r.headers["Content-Type"], body

    def test_page(self):
        status, ctype, body = self.get("/")
        self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn("<title>btlog</title>", body)
        self.assertIn("/api/hours", body)
        # Nothing from the database is baked into the page: it is all fetched
        # and escaped client-side, so a device called <script> stays text.
        self.assertNotIn("Garmin", body)
        self.assertIn("const esc = ", body)

    def test_api(self):
        status, ctype, body = self.get("/api/devices")
        self.assertEqual((status, ctype), (200, "application/json"))
        devices = json.loads(body)
        self.assertEqual([d["address"] for d in devices], [WATCH, BEACON])
        self.assertEqual(devices[1]["label"], "door")
        self.assertEqual(json.loads(self.get("/api/status")[2])["cycles"], 145)
        hours = json.loads(self.get("/api/hours?device=door&since=all")[2])
        self.assertEqual((len(hours), hours[3]), (24, 1.0))
        self.assertEqual(json.loads(self.get("/api/hours?since=all")[2])[7], 2.0)
        self.assertEqual(json.loads(self.get("/api/days?device=garmin&since=all")[2]).count(None), 5)
        timeline = json.loads(self.get("/api/timeline?device=garmin&since=all")[2])
        self.assertEqual((len(timeline), timeline[6]), (25, {"day": "2026-10-19", "hour": 6, "presence": 1.0}))
        self.assertEqual(devices[0]["vendor"], "")
        rssi = json.loads(self.get("/api/rssi?device=garmin&since=all")[2])
        self.assertEqual([r["when"] for r in rssi], ["2026-10-19", "2026-10-20"])
        self.assertEqual(json.loads(self.get("/api/new?since=2026-10-20")[2]), [])

    def test_bad_requests(self):
        for path, text in [("/api/hours?device=nobody", "no device matching"),
                           ("/api/hours?since=soon", "can't read time"),
                           ("/api/rssi", "device required"),
                           ("/api/rssi?device=garmin&by=week", "by must be"),
                           ("/api/new?since=all", "since required")]:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, 400, path)
            self.assertIn(text, cm.exception.read().decode())
            cm.exception.close()
        # Absurd but well-formed input is a 500 with a body, not a dropped connection.
        with self.assertRaises(urllib.error.HTTPError) as cm, self.assertLogs("btlog", level="ERROR"):
            self.get("/api/hours?since=99999999999999999999d")
        self.assertEqual(cm.exception.code, 500)
        cm.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()

    def test_foreign_host_header_is_refused(self):
        # A rebinding page reaches the server under its own DNS name.
        for host, code in [("evil.example:8778", 421), ("evil.example", 421), ("localhost:1", 200),
                           ("127.0.0.1", 200), ("[::1]:8778", 200), ("192.168.1.5", 200)]:
            req = urllib.request.Request(self.url + "/api/status", headers={"Host": host})
            try:
                with urllib.request.urlopen(req) as r:
                    self.assertEqual((host, r.status), (host, code))
                    self.assertIsNone(json.loads(r.read())["sightings"])
            except urllib.error.HTTPError as e:
                self.assertEqual((host, e.code), (host, code))
                e.close()

    def test_bind_parsing(self):
        with self.assertRaises(ValueError):
            web.make_server("x.db", "8778")


if __name__ == "__main__":
    unittest.main()
