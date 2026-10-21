import email.header
import json
import shlex
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from btlog import notify

ALERT = {"rule": "silent", "state": "open", "device": "C0:AA:BB:CC:DD:EE",
         "name": "freezer", "message": "freezer not heard for 6 h", "ts": 1792389600.0}


class Recorder(BaseHTTPRequestHandler):
    """Records every POST on the server and answers with server.status."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.command, self.path, self.headers, body))
        reply = b'{"error":"boom"}\n'
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def py(script, *args):
    """A command: target running script under this Python, quoted for shlex."""
    return "command:" + shlex.join([sys.executable, "-c", script, *map(str, args)])


class HttpTest(unittest.TestCase):
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

    def test_ntfy(self):
        notify.send(self.url + "/btlog-home", ALERT)
        notify.send(self.url + "/btlog-home", ALERT | {"state": "closed", "message": "freezer back"})
        (method, path, headers, body), closed = self.srv.seen
        self.assertEqual((method, path, body), ("POST", "/btlog-home", b"freezer not heard for 6 h"))
        self.assertEqual((headers["Title"], headers["Tags"]), ("btlog silent", "warning"))
        self.assertEqual((closed[2]["Tags"], closed[3]), ("white_check_mark", b"freezer back"))

    def test_ntfy_headers_are_safe(self):
        notify.send(self.url + "/t", {"message": "hi"})
        notify.send(self.url + "/t", {"rule": "silent\r\nTags: evil", "message": "x"})
        notify.send(self.url + "/t", {"rule": "frigo café", "message": "x"})
        plain, injected, accented = (h for _, _, h, _ in self.srv.seen)
        self.assertEqual((plain["Title"], plain["Tags"]), ("btlog", None))
        self.assertEqual((injected["Title"], injected.get_all("Tags")), ("btlog silent Tags: evil", None))
        (raw, charset), = email.header.decode_header(accented["Title"])
        self.assertEqual(raw.decode(charset), "btlog frigo café")

    def test_webhook(self):
        alert = ALERT | {"name": "frigo café"}
        notify.send("webhook:" + self.url + "/hook?x=1", alert)
        (method, path, headers, body), = self.srv.seen
        self.assertEqual((method, path, headers["Content-Type"]), ("POST", "/hook?x=1", "application/json"))
        self.assertEqual(json.loads(body), alert)

    def test_http_error(self):
        self.srv.status = 500
        with self.assertRaises(OSError) as cm:
            notify.send(self.url + "/t", ALERT)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertIn('{"error":"boom"}', str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))


class CommandTest(unittest.TestCase):
    def test_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "alert with space.json"
            script = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
            notify.send(py(script, out), ALERT)
            self.assertEqual(json.loads(out.read_bytes()), ALERT)

    def test_failure(self):
        script = "import sys; sys.stderr.write('no mailer\\nsecond line\\n'); sys.exit(3)"
        with self.assertRaises(OSError) as cm:
            notify.send(py(script), ALERT)
        self.assertRegex(str(cm.exception), r"exit status 3: no mailer$")

    def test_timeout(self):
        with self.assertRaises(TimeoutError):
            notify.send(py("import time; time.sleep(30)"), ALERT, timeout=0.5)

    def test_missing_program(self):
        with self.assertRaises(OSError):
            notify.send("command:/nonexistent/btlog-mailer", ALERT)


class CheckTest(unittest.TestCase):
    def test_forms(self):
        for good in ["https://ntfy.sh/btlog-home", "http://192.168.1.5:8080/t",
                     "webhook:https://ha.local/api/webhook/x", "command:/usr/bin/mail -s 'btlog alert' me@example.org"]:
            notify.check(good)
        for bad in ["ftp://x", "", "command:", "command:  ", "https://", "webhook:ftp://x",
                    "webhook:", "ntfy.sh/topic", "command:'unclosed"]:
            with self.assertRaises(ValueError, msg=bad) as cm:
                notify.check(bad)
            if bad != "command:'unclosed":
                self.assertIn("command:PROGRAM", str(cm.exception))
        with self.assertRaises(ValueError):
            notify.send("mailto:me@example.org", ALERT)


if __name__ == "__main__":
    unittest.main()
