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


if __name__ == "__main__":
    unittest.main()
