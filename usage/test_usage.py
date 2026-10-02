"""Tests for usage.py. Run with: python3 -m unittest discover -s usage -v

All names, hosts and printers here are made up.
"""

import os
import stat
import tempfile
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


if __name__ == "__main__":
    unittest.main()
