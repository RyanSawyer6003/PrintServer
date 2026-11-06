"""Tests for statuscheck.py. Run with: python3 -m unittest discover -s status -v

All names, hosts and printers here are made up.
"""

import json
import os
import socket
import stat
import tempfile
import unittest

import statuscheck

LPSTAT_P = """\
printer annex-office is idle.  enabled since Fri Oct  9 08:00:00 2026
\tForm mounted:
\tContent types: any
\tPrinter types: unknown
\tDescription: Annex office copier
\tAlerts: none
\tLocation: Annex, front office
\tConnection: direct
\tInterface: /etc/cups/ppd/annex-office.ppd
\tOn fault: no alert
\tAfter fault: continue
\tUsers allowed:
\t\t(all)
printer main-library now printing main-library-41.  enabled since Fri Oct  9 09:12:00 2026
\tDescription: Library: colour
\tLocation:
printer main-lab disabled since Fri Oct  9 07:30:00 2026 -
\tPaused
\tDescription: Lab laser
\tLocation: Main, room 12
"""

LPSTAT_V = """\
device for annex-office: socket://10.20.20.31:9100
device for main-library: ipps://lib-printer.example.org/ipp/print
device for main-lab: usb://Example/Laser?serial=123
"""

LPSTAT_A = """\
annex-office accepting requests since Fri Oct  9 08:00:00 2026
main-library accepting requests since Fri Oct  9 09:12:00 2026
main-lab not accepting requests since Fri Oct  9 07:30:00 2026 -
\tRejecting Jobs
"""

LPSTAT_O = """\
main-library-41         jdoe              1024   Fri Oct  9 09:12:00 2026
main-library-42         unknown           2048   Fri Oct  9 09:13:00 2026
annex-office-7          unknown           1024   Fri Oct  9 09:14:00 2026
gone-queue-3            unknown           1024   Fri Oct  9 09:14:00 2026
"""


class ParseTests(unittest.TestCase):
    def test_printers_state_description_and_location(self):
        queues = statuscheck.parse_printers(LPSTAT_P)
        self.assertEqual(list(queues), ["annex-office", "main-library", "main-lab"])
        self.assertEqual(queues["annex-office"],
                         {"state": "idle", "description": "Annex office copier",
                          "location": "Annex, front office"})
        self.assertEqual(queues["main-library"]["state"], "printing")
        self.assertEqual(queues["main-library"]["description"], "Library: colour")
        self.assertEqual(queues["main-library"]["location"], "")
        self.assertEqual(queues["main-lab"]["state"], "stopped")

    def test_devices_accepting_and_jobs(self):
        devices = statuscheck.parse_devices(LPSTAT_V)
        self.assertEqual(devices["annex-office"], "socket://10.20.20.31:9100")
        self.assertEqual(statuscheck.parse_accepting(LPSTAT_A),
                         {"annex-office": True, "main-library": True, "main-lab": False})
        names = {"annex-office", "main-library", "main-lab"}
        self.assertEqual(statuscheck.parse_jobs(LPSTAT_O, names),
                         {"main-library": 2, "annex-office": 1})

    def test_empty_output(self):
        self.assertEqual(statuscheck.parse_printers(""), {})
        self.assertEqual(statuscheck.parse_devices(""), {})
        self.assertEqual(statuscheck.parse_accepting(""), {})
        self.assertEqual(statuscheck.parse_jobs("", set()), {})

    def test_target_uses_the_port_the_queue_prints_to(self):
        cases = {
            "socket://10.20.20.31": ("10.20.20.31", 9100),
            "socket://10.20.20.31:9101": ("10.20.20.31", 9101),
            "ipp://printer.example.org/ipp/print": ("printer.example.org", 631),
            "ipps://printer.example.org/ipp/print": ("printer.example.org", 631),
            "ipp://user:secret@printer.example.org:8631/ipp": ("printer.example.org", 8631),
            "lpd://10.20.20.40/queue": ("10.20.20.40", 515),
            "http://10.20.20.41/ipp": ("10.20.20.41", 80),
            "https://10.20.20.41/ipp": ("10.20.20.41", 443),
            "socket://[fd00::31]:9100": ("fd00::31", 9100),
            "hp:/net/Example_Laser?ip=10.20.20.42": ("10.20.20.42", 9100),
            "hp:/net/Example_Laser?hostname=laser.example.org": ("laser.example.org", 9100),
        }
        for uri, expected in cases.items():
            self.assertEqual(statuscheck.target_of(uri), expected, uri)

    def test_target_is_none_when_there_is_no_host_to_connect_to(self):
        for uri in ("usb://Example/Laser?serial=123",
                    "dnssd://Example%20Laser._ipp._tcp.local/",
                    "implicitclass://lab/",
                    "file:/dev/null",
                    "smb://server/share",
                    "hp:/usb/Example_Laser?serial=1",
                    "socket://",
                    "socket://host:notaport",
                    "socket://bad host:9100",
                    "socket://10.20.20.31:0",
                    ""):
            self.assertIsNone(statuscheck.target_of(uri), uri)


class CheckTests(unittest.TestCase):
    def test_port_check_against_a_listener_and_a_closed_port(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        open_port = listener.getsockname()[1]

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()

        results = statuscheck.check_all({"a": ("127.0.0.1", open_port),
                                         "b": ("127.0.0.1", closed_port),
                                         "c": ("no-such-host.invalid", 9100),
                                         "d": None}, timeout=2)
        self.assertEqual(results, {"a": True, "b": False, "c": False, "d": None})


def queue(**extra):
    return dict({"state": "idle", "description": "", "location": "", "accepting": True, "jobs": 0},
                **extra)


class MergeTests(unittest.TestCase):
    def step(self, previous, answer, now, down_after=2):
        return statuscheck.merge(previous, {"q": queue()}, {"q": answer}, now, down_after)

    def test_down_only_after_two_failed_checks_in_a_row(self):
        first = self.step([], True, 100)
        self.assertEqual((first[0]["check"], first[0]["since"], first[0]["last_up"]), ("up", 100, 100))

        missed = self.step(first, False, 400)
        self.assertEqual((missed[0]["check"], missed[0]["since"], missed[0]["failures"]), ("up", 100, 1))

        down = self.step(missed, False, 700)
        # "Down since" is the first failed check, not the one that confirmed it.
        self.assertEqual((down[0]["check"], down[0]["since"], down[0]["last_up"]), ("down", 400, 100))

        still = self.step(down, False, 1000)
        self.assertEqual((still[0]["check"], still[0]["since"], still[0]["failures"]), ("down", 400, 3))

        back = self.step(still, True, 1300)
        self.assertEqual((back[0]["check"], back[0]["since"], back[0]["failures"]), ("up", 1300, 0))
        self.assertIsNone(back[0]["failing_since"])

    def test_one_missed_check_does_not_flag_a_printer(self):
        up = self.step([], True, 100)
        missed = self.step(up, False, 400)
        again = self.step(missed, True, 700)
        self.assertEqual((again[0]["check"], again[0]["since"], again[0]["failures"]), ("up", 100, 0))

    def test_new_queue_that_fails_is_unknown_until_confirmed(self):
        first = self.step([], False, 100)
        self.assertEqual((first[0]["check"], first[0]["since"]), ("unknown", None))
        second = self.step(first, False, 400)
        self.assertEqual((second[0]["check"], second[0]["since"]), ("down", 100))

    def test_queue_without_an_address_is_unchecked_and_removed_queues_drop_out(self):
        previous = [{"name": "old", "check": "up", "since": 1}]
        merged = statuscheck.merge(previous, {"q": queue(state="stopped")}, {"q": None}, 100, 2)
        self.assertEqual([m["name"] for m in merged], ["q"])
        self.assertEqual((merged[0]["check"], merged[0]["since"], merged[0]["state"]),
                         ("unchecked", None, "stopped"))

    def test_damaged_previous_state_is_ignored(self):
        merged = statuscheck.merge(["junk", {"no": "name"}], {"q": queue()}, {"q": True}, 100, 2)
        self.assertEqual(merged[0]["check"], "up")


class FileTests(unittest.TestCase):
    def test_saved_file_is_world_readable_and_holds_no_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            self.assertEqual(statuscheck.load(path), [])
            queues = statuscheck.parse_printers(LPSTAT_P)
            for info in queues.values():
                info.update(accepting=True, jobs=0)
            merged = statuscheck.merge([], queues, dict.fromkeys(queues, True), 100, 2)
            statuscheck.save(path, {"version": 1, "checked_at": 100, "interval": 300, "queues": merged})

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
            self.assertEqual(os.listdir(tmp), ["status.json"])
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertEqual(len(json.loads(text)["queues"]), 3)
            for secret in ("10.20.20", "example.org", "socket", "ipps", "usb:"):
                self.assertNotIn(secret, text)
            self.assertEqual([q["name"] for q in statuscheck.load(path)], list(queues))

            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(statuscheck.load(path), [])


def entry(name="q", check="up", state="idle", since=100, **extra):
    return dict({"name": name, "check": check, "state": state, "since": since,
                 "description": "Annex office copier", "location": "Annex, room 4"}, **extra)


class ChangeTests(unittest.TestCase):
    def test_up_to_down_and_back(self):
        down = statuscheck.changes([entry()], [entry(check="down", since=400)])
        self.assertEqual([(e["event"], e["previous"], e["current"], e["since"]) for e in down],
                         [("down", "up", "down", 400)])
        up = statuscheck.changes([entry(check="down")], [entry(check="up", since=900)])
        self.assertEqual([(e["event"], e["previous"], e["current"]) for e in up],
                         [("up", "down", "up")])

    def test_no_event_while_state_holds_or_before_confirmed(self):
        self.assertEqual(statuscheck.changes([entry()], [entry()]), [])
        self.assertEqual(statuscheck.changes([entry(check="down")], [entry(check="down")]), [])
        # One missed check keeps the printer "up": not news yet.
        self.assertEqual(statuscheck.changes([entry()], [entry(failures=1)]), [])

    def test_first_sight_is_a_baseline(self):
        self.assertEqual(statuscheck.changes([], [entry(), entry("s", state="stopped")]), [])
        # unknown -> up is the baseline completing, not a recovery.
        self.assertEqual(statuscheck.changes([entry(check="unknown")], [entry()]), [])

    def test_never_seen_up_still_raises_down(self):
        events = statuscheck.changes([entry(check="unknown")], [entry(check="down")])
        self.assertEqual([(e["event"], e["previous"]) for e in events], [("down", "unknown")])

    def test_queue_stopped_and_resumed(self):
        stopped = statuscheck.changes([entry()], [entry(state="stopped")])
        self.assertEqual([(e["event"], e["previous"]) for e in stopped], [("stopped", "idle")])
        resumed = statuscheck.changes([entry(state="stopped")], [entry(state="printing")])
        self.assertEqual([(e["event"], e["current"]) for e in resumed], [("resumed", "printing")])

    def test_unchecked_queue_and_removed_queue(self):
        self.assertEqual(statuscheck.changes([entry(check="unchecked")],
                                             [entry(check="unchecked")]), [])
        self.assertEqual(statuscheck.changes([entry("gone", check="down")], []), [])

    def test_both_changes_on_one_queue(self):
        events = statuscheck.changes([entry()], [entry(check="down", state="stopped")])
        self.assertEqual([e["event"] for e in events], ["down", "stopped"])

    def test_log_line(self):
        [event] = statuscheck.changes([entry()], [entry(check="down")])
        self.assertEqual(statuscheck.describe(event),
                         "printer status change: q up -> down (Annex office copier, Annex, room 4)")
        [event] = statuscheck.changes([entry(description="", location="")],
                                      [entry(state="stopped", description="", location="")])
        self.assertEqual(statuscheck.describe(event), "printer status change: q queue idle -> stopped")


class PayloadTests(unittest.TestCase):
    def events(self):
        return statuscheck.changes([entry("a"), entry("b")],
                                   [entry("a", check="down", since=86400),
                                    entry("b", check="down", since=86400)])

    def test_json_holds_every_event_in_one_message(self):
        body = statuscheck.payload(self.events(), 86460, "json", "printserver.example.org")
        self.assertEqual((body["version"], body["server"], body["checked_at"]),
                         (1, "printserver.example.org", "1970-01-02T00:01:00Z"))
        self.assertEqual([e["queue"] for e in body["events"]], ["a", "b"])
        self.assertEqual(body["events"][0]["since"], "1970-01-02T00:00:00Z")
        json.dumps(body)

    def test_teams_card(self):
        body = statuscheck.payload(self.events(), 86460, "teams")
        self.assertEqual(body["type"], "message")
        attachment = body["attachments"][0]
        self.assertEqual(attachment["contentType"], "application/vnd.microsoft.card.adaptive")
        card = attachment["content"]
        self.assertEqual((card["type"], card["body"][0]["text"]),
                         ("AdaptiveCard", "2 printer status changes"))
        single = statuscheck.payload(self.events()[:1], 86460, "teams", "srv")
        texts = [b.get("text") for b in single["attachments"][0]["content"]["body"]]
        self.assertEqual(texts[:2], ["Printer down: a", "srv"])
        json.dumps(body)

    def test_no_address_in_payload(self):
        events = statuscheck.changes([entry()], [entry(check="down")])
        for fmt in statuscheck.WEBHOOK_FORMATS:
            self.assertNotIn("127.0.0.1", json.dumps(statuscheck.payload(events, 1, fmt)))


class WebhookTests(unittest.TestCase):
    def setUp(self):
        import http.server
        import threading
        received = self.received = []
        status = self.status = [204]

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append((self.headers["Content-Type"], json.loads(self.rfile.read(length))))
                self.send_response(status[0])
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook?sig=secret"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_send_posts_json(self):
        self.assertIsNone(statuscheck.send(self.url, {"x": 1}))
        self.assertEqual(self.received, [("application/json", {"x": 1})])

    def test_errors_never_contain_the_url(self):
        self.status[0] = 500
        self.assertEqual(statuscheck.send(self.url, {}), "HTTP 500")
        self.server.shutdown()
        self.server.server_close()
        error = statuscheck.send(self.url, {}, timeout=2)
        self.assertIsNotNone(error)
        self.assertNotIn("secret", error)
        self.assertNotIn("hook", error)

    def test_one_message_per_check(self):
        events = statuscheck.changes([entry("a"), entry("b")],
                                     [entry("a", check="down"), entry("b", state="stopped")])
        statuscheck.report(events, 100, (self.url, "json"))
        self.assertEqual(len(self.received), 1)
        self.assertEqual([e["event"] for e in self.received[0][1]["events"]], ["down", "stopped"])

    def test_nothing_sent_without_changes_or_url(self):
        statuscheck.report([], 100, (self.url, "json"))
        events = statuscheck.changes([entry()], [entry(check="down")])
        statuscheck.report(events, 100, (None, "json"))
        self.assertEqual(self.received, [])

    def test_cycle_saves_then_reports(self):
        answers = iter([True, False, False])
        saved_before_send = []

        def fake_send(url, body, timeout=10):
            saved_before_send.append(statuscheck.load(path)[0]["check"])
            self.received.append(("fake", body))

        originals = (statuscheck.read_queues, statuscheck.check_all, statuscheck.send)
        statuscheck.read_queues = lambda: ({"q": queue()}, {"q": ("127.0.0.1", 9)})
        statuscheck.check_all = lambda targets, timeout: {"q": next(answers)}
        statuscheck.send = fake_send
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "status.json")
                for _ in range(3):
                    statuscheck.cycle(path, 300, 1, 2, ("http://hooks.example.org/x", "json"))
        finally:
            statuscheck.read_queues, statuscheck.check_all, statuscheck.send = originals
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0][1]["events"][0]["event"], "down")
        self.assertEqual(saved_before_send, ["down"])

    def test_settings(self):
        saved = {k: os.environ.pop(k, None) for k in ("STATUS_WEBHOOK_URL", "STATUS_WEBHOOK_FORMAT")}
        try:
            self.assertEqual(statuscheck.webhook_settings(), (None, "json"))
            os.environ.update(STATUS_WEBHOOK_URL=" https://hooks.example.org/x ",
                              STATUS_WEBHOOK_FORMAT="Teams")
            self.assertEqual(statuscheck.webhook_settings(),
                             ("https://hooks.example.org/x", "teams"))
            os.environ["STATUS_WEBHOOK_FORMAT"] = "slack"
            with self.assertRaises(SystemExit):
                statuscheck.webhook_settings()
            os.environ.update(STATUS_WEBHOOK_FORMAT="json", STATUS_WEBHOOK_URL="ftp://x")
            with self.assertRaises(SystemExit) as raised:
                statuscheck.webhook_settings()
            self.assertNotIn("ftp://x", str(raised.exception))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
