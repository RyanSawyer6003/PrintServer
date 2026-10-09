"""Tests for usage.py. Run with: python3 -m unittest discover -s usage -v

All names, hosts and printers here are made up.
"""

import json
import os
import stat
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

import usage

UTC = ZoneInfo("UTC")
MOUNTAIN = ZoneInfo("America/Denver")

# CUPS default PageLogFormat, as written before this project's format is deployed.
DEFAULT_LOCAL = "lobby printadmin 1 [02/Oct/2026:14:46:14 +0000] total 1 - localhost Test Page - -"
DEFAULT_WINDOWS = ("lobby AzureAD\\JaneDoe 2 [02/Oct/2026:15:01:38 +0000] total 1 - 10.30.3.139 "
                   "Test Page  one-sided")
# This project's PageLogFormat.
PIPE = ("lobby|AzureAD\\JaneDoe|5|[03/Oct/2026:15:01:38 +0000]|total|6|10.30.3.139|"
        "two-sided-long-edge|color|Budget | draft.pdf")


class ParseTests(unittest.TestCase):
    def test_default_format_local_job(self):
        job = usage.parse_line(DEFAULT_LOCAL)
        self.assertEqual((job["printer"], job["user_name"], job["job_id"], job["pages"]),
                         ("lobby", "printadmin", 1, 1))
        self.assertEqual((job["host"], job["title"], job["sides"]), ("localhost", "Test Page", ""))

    def test_default_format_windows_job_with_empty_media(self):
        job = usage.parse_line(DEFAULT_WINDOWS)
        self.assertEqual((job["user_key"], job["user_name"], job["user_raw"]),
                         ("janedoe", "JaneDoe", "AzureAD\\JaneDoe"))
        self.assertEqual((job["host"], job["title"], job["sides"], job["pages"]),
                         ("10.30.3.139", "Test Page", "one-sided", 1))

    def test_pipe_format_keeps_delimiter_in_job_name(self):
        job = usage.parse_line(PIPE)
        self.assertEqual((job["pages"], job["sides"], job["color"]), (6, "two-sided-long-edge", "color"))
        self.assertEqual(job["title"], "Budget | draft.pdf")
        self.assertEqual(job["user_name"], "JaneDoe")

    def test_same_person_groups_regardless_of_prefix_and_case(self):
        keys = {usage.normalize_user(n)[0] for n in ("AzureAD\\JaneDoe", "janedoe", "CORP\\JANEDOE")}
        self.assertEqual(keys, {"janedoe"})
        self.assertEqual(usage.normalize_user("-"), ("(unknown)", "(unknown)"))

    def test_user_name_with_spaces(self):
        line = "lobby Front Desk 7 [02/Oct/2026:15:01:38 +0000] total 3 - 10.30.3.9 Flyer - one-sided"
        job = usage.parse_line(line)
        self.assertEqual((job["user_name"], job["job_id"], job["pages"]), ("Front Desk", 7, 3))

    def test_timestamp_offsets_and_microseconds(self):
        base = usage.parse_timestamp("[02/Oct/2026:15:00:00 +0000]")
        self.assertEqual(usage.parse_timestamp("[02/Oct/2026:10:00:00 -0500]"), base)
        self.assertEqual(usage.parse_timestamp("[02/Oct/2026:15:00:00.123456 +0000]"), base)
        self.assertIsNone(usage.parse_timestamp("[32/Oct/2026:15:00:00 +0000]"))
        self.assertIsNone(usage.parse_timestamp("yesterday"))

    def test_lines_that_are_not_job_totals_are_rejected(self):
        for line in ("garbage",
                     "lobby jdoe 9 [02/Oct/2026:15:01:38 +0000] 1 1 - host Doc - -",   # per-page line
                     "lobby jdoe x [02/Oct/2026:15:01:38 +0000] total 1 - host Doc - -",
                     "lobby|jdoe|9|[bad time]|total|1|host|-|-|Doc",
                     "lobby|jdoe|9|[02/Oct/2026:15:01:38 +0000]|total|-4|host|-|-|Doc"):
            self.assertIsNone(usage.parse_line(line), line)
        self.assertIsNone(usage.parse_line("   "))


class StoreAndReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")
        self.out = os.path.join(self.tmp.name, "reports")
        os.makedirs(self.logs)
        self.db = usage.open_db(os.path.join(self.tmp.name, "usage.db"))
        self.addCleanup(self.db.close)

    def write_log(self, name, lines):
        with open(os.path.join(self.logs, name), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def test_ingest_is_idempotent_and_reads_the_rotated_log(self):
        self.write_log("page_log.O", [DEFAULT_LOCAL])
        self.write_log("page_log", [DEFAULT_WINDOWS, PIPE, "not a log line"])
        changed, unreadable = usage.ingest(self.db, UTC, self.logs)
        self.assertEqual((self.count(), changed, unreadable), (3, {"2026-10"}, 1))
        changed, unreadable = usage.ingest(self.db, UTC, self.logs)
        self.assertEqual((self.count(), changed, unreadable), (3, set(), 1))

    def test_missing_logs_are_not_an_error(self):
        self.assertEqual(usage.ingest(self.db, UTC, self.logs), (set(), 0))

    def test_month_follows_the_local_time_zone(self):
        # 03:00 UTC on Nov 1 is still Oct 31 in US Mountain time.
        self.write_log("page_log", ["lobby|jdoe|1|[01/Nov/2026:03:00:00 +0000]|total|2|h|-|-|Doc"])
        usage.ingest(self.db, MOUNTAIN, self.logs)
        self.assertEqual(self.db.execute("SELECT month FROM jobs").fetchone()[0], "2026-10")

    def test_report_flags_people_over_allowance_and_honours_overrides(self):
        self.write_log("page_log", [
            "lobby|AzureAD\\JaneDoe|1|[02/Oct/2026:15:00:00 +0000]|total|40|h1|one-sided|color|A",
            "lobby|janedoe|2|[03/Oct/2026:15:00:00 +0000]|total|20|h1|one-sided|color|B",
            "annex|AzureAD\\SamLee|3|[03/Oct/2026:16:00:00 +0000]|total|60|h2|one-sided|monochrome|C",
        ])
        usage.ingest(self.db, UTC, self.logs)
        users = {u["key"]: u for u in usage.user_totals(self.db, "2026-10", 50)}
        self.assertEqual((users["janedoe"]["pages"], users["janedoe"]["jobs"]), (60, 2))
        self.assertTrue(users["janedoe"]["over"] and users["samlee"]["over"])

        self.db.execute("INSERT INTO allowances VALUES ('samlee', 100)")
        users = {u["key"]: u for u in usage.user_totals(self.db, "2026-10", 50)}
        self.assertFalse(users["samlee"]["over"])
        self.assertTrue(users["samlee"]["custom"])

        no_limit = usage.user_totals(self.db, "2026-10", 0)
        self.assertFalse(any(u["over"] for u in no_limit))

    def test_rendered_files_are_escaped_and_world_readable(self):
        self.write_log("page_log", [
            "lobby|<script>alert(1)</script>|1|[02/Oct/2026:15:00:00 +0000]|total|3|h|-|-|<b>x</b> & y",
            "lobby|jdoe|2|[02/Oct/2026:16:00:00 +0000]|total|1|h|-|-|=HYPERLINK(\"http://x\")",
        ])
        usage.ingest(self.db, UTC, self.logs)
        usage.render(self.db, UTC, 500, 0, out_dir=self.out)

        names = set(os.listdir(self.out))
        self.assertTrue({"index.html", "2026-10.html", "2026-10-jobs.csv", "2026-10-people.csv"} <= names)
        self.assertFalse(any(n.endswith(".tmp") for n in names))

        with open(os.path.join(self.out, "2026-10.html"), encoding="utf-8") as handle:
            page = handle.read()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", page)

        with open(os.path.join(self.out, "2026-10-jobs.csv"), encoding="utf-8", newline="") as handle:
            jobs_csv = handle.read()
        self.assertIn("'=HYPERLINK", jobs_csv)

        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o755)
        for name in names:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o644, name)

    def test_empty_month_still_renders_an_index(self):
        usage.render(self.db, UTC, 500, 0, out_dir=self.out)
        with open(os.path.join(self.out, "index.html"), encoding="utf-8") as handle:
            self.assertIn("No print jobs recorded this month.", handle.read())


def status_queue(name, **extra):
    return dict({"name": name, "description": "", "location": "", "state": "idle", "accepting": True,
                 "jobs": 0, "check": "up", "since": 1000}, **extra)


class StaffPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")
        self.out = os.path.join(self.tmp.name, "staff")
        os.makedirs(self.logs)
        self.db = usage.open_db(os.path.join(self.tmp.name, "usage.db"))
        self.addCleanup(self.db.close)

    def write_log(self, lines):
        with open(os.path.join(self.logs, "page_log"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        usage.ingest(self.db, UTC, self.logs)

    def write_status(self, document):
        path = os.path.join(self.tmp.name, "status.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(document if isinstance(document, str) else json.dumps(document))
        return path

    def page(self, status, name="index.html"):
        usage.render_staff(self.db, UTC, status, out_dir=self.out)
        with open(os.path.join(self.out, name), encoding="utf-8") as handle:
            return handle.read()

    def this_month(self, day_time="02 15:00:00"):
        stamp = time.strftime("%b/%Y", time.gmtime())
        day, clock = day_time.split()
        return f"[{day}/{stamp}:{clock} +0000]"

    def test_printers_are_listed_by_location_then_description(self):
        queues = {
            "20417": {"location": "North, room 9", "description": "Laser"},
            "9031": {"location": "North, room 10", "description": "Laser"},
            "20500": {"location": "", "description": "Unplaced"},
            "20007": {"location": "annex, office", "description": "Copier"},
            "9500": {"location": "Annex, office", "description": "Colour laser"},
        }
        self.assertEqual(usage.by_location(queues), ["9500", "20007", "20417", "9031", "20500"])

        status = {"checked_at": int(time.time()), "interval": 300, "queues": [
            status_queue(name, **info) for name, info in queues.items()]}
        page = self.page(status)
        rows = [page.index(f"<td class=\"id\">{name}</td>") for name in usage.by_location(queues)]
        self.assertEqual(rows, sorted(rows))
        self.assertIn("No location set", page)

    def test_status_file_is_checked_before_use(self):
        self.assertIsNone(usage.load_status(os.path.join(self.tmp.name, "missing.json")))
        self.assertIsNone(usage.load_status(self.write_status("{not json")))
        self.assertIsNone(usage.load_status(self.write_status({"queues": []})))
        status = usage.load_status(self.write_status({
            "checked_at": 2000, "interval": "soon",
            "queues": ["junk", {"name": ""}, {"name": "north-a", "check": "exploded", "state": 7,
                                              "jobs": -3, "since": "then", "accepting": False}]}))
        self.assertEqual(status["interval"], 300)
        self.assertEqual(status["queues"], [{
            "name": "north-a", "description": "", "location": "", "state": "idle",
            "accepting": False, "jobs": 0, "check": "unchecked", "since": None}])

    def test_staff_page_never_shows_people_documents_or_computers(self):
        self.write_log([
            f"20417|AzureAD\\JaneDoe|1|{self.this_month()}|total|40|10.30.3.139|one-sided|color|SecretTitle.docx",
            f"9031|AzureAD\\SamLee|2|{self.this_month()}|total|60|laptop-77|one-sided|color|Payroll.xlsx",
            f"20417|AzureAD\\SamLee|3|{self.this_month()}|total|5|laptop-77|one-sided|color|Notes.docx",
            f"8000|AzureAD\\SamLee|4|{self.this_month()}|total|2|laptop-77|one-sided|color|Old.docx",
        ])
        status = {"checked_at": int(time.time()), "interval": 300, "queues": [
            status_queue("20417", description="Colour laser", location="North, library"),
            status_queue("9031", description="Copier", location="South, office")]}
        page = self.page(status)
        for hidden in ("JaneDoe", "SamLee", "SecretTitle", "Payroll", "Notes.docx", "10.30.3.139",
                       "laptop-77", "AzureAD", ".csv", "llowance", "By person"):
            self.assertNotIn(hidden, page)
        self.assertIn("All 2 printers are up", page)
        # Totals per printer, busiest first, with where each printer is.
        self.assertIn("<tr><td>South, office</td><td>Copier</td><td>9031</td>"
                      "<td class=\"num\">60</td><td class=\"num\">1</td></tr>", page)
        self.assertIn("<tr><td>North, library</td><td>Colour laser</td><td>20417</td>"
                      "<td class=\"num\">45</td><td class=\"num\">2</td></tr>", page)
        # A printer that is no longer on the server still has its totals.
        self.assertIn("<tr><td></td><td></td><td>8000</td><td class=\"num\">2</td>", page)
        self.assertLess(page.index("<td>9031</td>"), page.index("<td>20417</td>"))

    def test_headline_counts_down_printers_and_leaves_out_unchecked_ones(self):
        now = int(time.time())
        status = {"checked_at": now, "interval": 300, "queues": [
            status_queue("north-a"),
            status_queue("north-b", check="down", since=now - 600),
            status_queue("south-c", check="unchecked", since=None, state="stopped", accepting=False),
        ]}
        page = self.page(status)
        self.assertIn("1 of 2 printers is down", page)
        self.assertIn("Down: north-b.", page)
        self.assertNotIn("<h3", page)
        self.assertIn("1 printer can't be checked", page)
        self.assertIn("Paused</span>, not accepting jobs", page)
        self.assertNotIn("The checks have stopped", page)

    def test_a_printer_still_being_checked_is_not_counted_as_up(self):
        status = {"checked_at": int(time.time()), "interval": 300, "queues": [
            status_queue("north-a"), status_queue("north-b", check="unknown", since=None)]}
        page = self.page(status)
        self.assertIn("1 of 2 printers is up", page)
        self.assertIn("Still checking: north-b.", page)
        self.assertNotIn("All 2 printers are up", page)

    def test_down_printers_are_named_with_their_location(self):
        now = int(time.time())
        status = {"checked_at": now, "interval": 300, "queues": [
            status_queue("20417", check="down", since=now - 600, location="North, library"),
            status_queue("9031", check="down", since=now - 600, description="Copier"),
            status_queue("9500")]}
        self.assertIn("Down: 20417 (North, library), 9031 (Copier).", self.page(status))

    def test_missing_and_stale_status_are_said_plainly(self):
        self.assertIn("Printer status isn't available yet", self.page(None))
        stale = {"checked_at": int(time.time()) - 3600, "interval": 300, "queues": [status_queue("a")]}
        self.assertIn("The checks have stopped", self.page(stale))
        self.assertIn("No printers are set up yet",
                      self.page({"checked_at": int(time.time()), "interval": 300, "queues": []}))

    def test_queue_text_is_escaped(self):
        status = {"checked_at": int(time.time()), "interval": 300, "queues": [
            status_queue("north-a", description="<img src=x>", location="Room \"1\" & 2")]}
        page = self.page(status)
        self.assertNotIn("<img", page)
        self.assertIn("<td class=\"loc\">Room &quot;1&quot; &amp; 2</td>"
                      "<td class=\"desc\">&lt;img src=x&gt;</td>", page)

    def test_earlier_months_get_their_own_page_without_status(self):
        self.write_log(["north-lab|jdoe|1|[02/Jan/2020:15:00:00 +0000]|total|5|h|-|-|Doc"])
        status = {"checked_at": int(time.time()), "interval": 300, "queues": [status_queue("north-lab")]}
        index = self.page(status)
        self.assertIn("href=\"/status/2020-01.html\"", index)
        with open(os.path.join(self.out, "2020-01.html"), encoding="utf-8") as handle:
            january = handle.read()
        self.assertIn("Pages printed in January 2020", january)
        self.assertNotIn("jdoe", january)
        self.assertIn("<td>north-lab</td><td class=\"num\">5</td>", january)
        self.assertNotIn("printers are up", january)
        self.assertNotIn("http-equiv=\"refresh\"", january)
        for name in os.listdir(self.out):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o644, name)


if __name__ == "__main__":
    unittest.main()
