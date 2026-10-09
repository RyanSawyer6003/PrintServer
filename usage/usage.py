#!/usr/bin/env python3
"""Print usage reporting for a CUPS server.

Reads the CUPS page_log, keeps one row per print job in SQLite, and writes
static pages for CUPS to serve:

  /usage/   the full report with the job log and CSV downloads (login required)
  /status/  the staff page: printer status and page totals, with no document
            names, computer addresses or job log (no login)

Report only: this never blocks or changes a print job. It has no network
access and only reads the log files and the printer status file.

Commands:
    usage run                      ingest and render in a loop (the service)
    usage once                     ingest and render one time
    usage allowance list           show the default and per-user allowances
    usage allowance set USER N     give USER a monthly allowance of N pages
    usage allowance clear USER     return USER to the default allowance
"""

import argparse
import csv
import html
import io
import json
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG_DIR = os.environ.get("USAGE_LOG_DIR", "/var/log/cups")
DB_PATH = os.environ.get("USAGE_DB", "/data/usage.db")
OUT_DIR = os.environ.get("USAGE_REPORT_DIR", "/reports")
HEARTBEAT = os.environ.get("USAGE_HEARTBEAT", "/data/heartbeat")
STAFF_DIR = os.environ.get("USAGE_STAFF_DIR", "/staff")
STATUS_FILE = os.environ.get("USAGE_STATUS_FILE", "/status-data/status.json")
RECENT_JOBS = 100

MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}

# The format this project sets with PageLogFormat in cupsd.conf.template:
#   %p|%u|%j|%T|%P|%C|%{job-originating-host-name}|%{sides}|%{print-color-mode}|%{job-name}
# The job name is last because it is free text and may contain the delimiter.
PIPE_FIELDS = 10

# The CUPS default format, for lines written before the template was deployed:
#   %p %u %j %T %P %C %{job-billing} %{job-originating-host-name} %{job-name} %{media} %{sides}
DEFAULT_RE = re.compile(
    r"^(?P<printer>\S+) (?P<user>.+?) (?P<job>\d+) \[(?P<ts>[^\]]+)\] "
    r"(?P<page>\S+) (?P<count>-?\d+) (?P<rest>.*)$")

TS_RE = re.compile(
    r"^(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?:\.\d+)? ([+-])(\d{2})(\d{2})$")


# ----------------------------------------------------------------- settings

def env_int(name, default=0):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"ERROR: {name} must be a whole number, got {raw!r}")
    if value < 0:
        sys.exit(f"ERROR: {name} must not be negative, got {raw!r}")
    return value


def local_zone():
    name = (os.environ.get("TZ") or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"ERROR: TZ={name!r} is not a known time zone (use a name like America/New_York)")


# ------------------------------------------------------------------ parsing

def parse_timestamp(text):
    """'[02/Oct/2026:14:46:14 +0000]' (brackets optional) -> epoch seconds."""
    m = TS_RE.match(text.strip().strip("[]"))
    if not m or m.group(2).title() not in MONTHS:
        return None
    day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
    offset = timedelta(hours=int(oh), minutes=int(om))
    if sign == "-":
        offset = -offset
    try:
        when = datetime(int(year), MONTHS[mon.title()], int(day), int(hh), int(mm), int(ss),
                        tzinfo=timezone(offset))
    except ValueError:
        return None
    return int(when.timestamp())


def normalize_user(raw):
    """'AzureAD\\JaneDoe' -> ('janedoe', 'JaneDoe'). The key groups a person's jobs."""
    name = raw.strip()
    if "\\" in name:
        name = name.rsplit("\\", 1)[1].strip()
    if not name or name == "-":
        name = "(unknown)"
    return name.casefold(), name


def clean(value):
    value = value.strip()
    return "" if value in ("-", "???") else value


def parse_line(line):
    """Return a job dict, or None if the line is not a job total we can read.

    CUPS writes one line per finished job with the page field set to 'total'
    and the count field holding the job's impressions (pages x copies).
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

    parts = line.split("|", PIPE_FIELDS - 1)
    if len(parts) == PIPE_FIELDS and parts[2].isdigit() and parts[3].startswith("["):
        printer, user, job, ts, page, count, host, sides, color, title = parts
    else:
        m = DEFAULT_RE.match(line)
        if not m:
            return None
        printer, user, job = m.group("printer"), m.group("user"), m.group("job")
        ts, page, count = m.group("ts"), m.group("page"), m.group("count")
        rest = m.group("rest").split(" ")
        if len(rest) < 4:
            return None
        host, sides, color = rest[1], rest[-1], ""
        title = " ".join(rest[2:-2])

    when = parse_timestamp(ts)
    if when is None or page.strip().lower() != "total":
        return None
    try:
        pages = int(count)
    except ValueError:
        return None
    if pages < 0:
        return None

    user_key, user_name = normalize_user(user)
    return {
        "printer": printer.strip(), "job_id": int(job), "ts": when,
        "user_key": user_key, "user_name": user_name, "user_raw": user.strip(),
        "pages": pages, "host": clean(host), "title": title.strip(),
        "sides": clean(sides), "color": clean(color),
    }


# ----------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY,
    printer   TEXT    NOT NULL,
    job_id    INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    month     TEXT    NOT NULL,
    user_key  TEXT    NOT NULL,
    user_name TEXT    NOT NULL,
    user_raw  TEXT    NOT NULL,
    pages     INTEGER NOT NULL,
    host      TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL DEFAULT '',
    sides     TEXT    NOT NULL DEFAULT '',
    color     TEXT    NOT NULL DEFAULT '',
    UNIQUE (printer, job_id, ts)
);
CREATE INDEX IF NOT EXISTS jobs_month ON jobs (month);
CREATE TABLE IF NOT EXISTS allowances (
    user_key TEXT PRIMARY KEY,
    pages    INTEGER NOT NULL
);
"""


def open_db(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def month_of(ts, zone):
    return datetime.fromtimestamp(ts, zone).strftime("%Y-%m")


def ingest(db, zone, log_dir=None):
    """Read page_log.O then page_log. Returns (months that gained jobs, unreadable lines)."""
    changed, unreadable = set(), 0
    for name in ("page_log.O", "page_log"):
        path = os.path.join(log_dir or LOG_DIR, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            continue
        for line in lines:
            if not line.strip():
                continue
            job = parse_line(line)
            if job is None:
                unreadable += 1
                continue
            job["month"] = month_of(job["ts"], zone)
            cur = db.execute(
                "INSERT OR IGNORE INTO jobs (printer, job_id, ts, month, user_key, user_name,"
                " user_raw, pages, host, title, sides, color) VALUES (:printer, :job_id, :ts,"
                " :month, :user_key, :user_name, :user_raw, :pages, :host, :title, :sides, :color)",
                job)
            if cur.rowcount:
                changed.add(job["month"])
    db.commit()
    return changed, unreadable


# ---------------------------------------------------------------- reporting

CSS = """
:root { color-scheme: light dark; --fg:#1c2128; --muted:#5b6470; --bg:#ffffff; --panel:#f4f6f8;
  --line:#d8dde3; --accent:#1f5fa8; --over:#a4262c; --overbg:#fdecea; --bar:#1f5fa8; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6e9ed; --muted:#9aa4b1; --bg:#15181c;
  --panel:#1e2329; --line:#333b45; --accent:#7ab3f0; --over:#ff9c94; --overbg:#3a1f1e; --bar:#7ab3f0; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:1080px; margin:0 auto; }
h1 { font-size:24px; margin:0 0 4px; }
h2 { font-size:17px; margin:32px 0 8px; }
p.note { color:var(--muted); margin:0 0 12px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin:20px 0 8px; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
.tile b { display:block; font-size:24px; line-height:1.2; }
.tile span { color:var(--muted); font-size:13px; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
tr:last-child td { border-bottom:0; }
th { background:var(--panel); font-size:13px; color:var(--muted); font-weight:600; }
td.num, th.num { text-align:right; }
td.doc { white-space:normal; min-width:260px; overflow-wrap:anywhere; }
tr.over td { background:var(--overbg); }
.status-over { color:var(--over); font-weight:600; }
.meter { display:inline-block; width:90px; height:8px; background:var(--line); border-radius:4px;
  vertical-align:middle; margin-right:8px; overflow:hidden; }
.meter i { display:block; height:100%; background:var(--bar); }
tr.over .meter i { background:var(--over); }
nav, .links { margin:4px 0 0; display:flex; flex-wrap:wrap; gap:4px 14px; }
nav span, .links span { color:var(--muted); }
a { color:var(--accent); }
nav a.here { color:var(--fg); font-weight:600; text-decoration:none; }
footer { margin-top:36px; color:var(--muted); font-size:13px; }
.empty { color:var(--muted); padding:14px 12px; }
"""


def esc(value):
    return html.escape(str(value), quote=True)


def month_title(month):
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def csv_cell(value):
    """Stop spreadsheet apps from running a cell that starts like a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def csv_text(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_cell(v) if isinstance(v, str) else v for v in row])
    return out.getvalue()


def write_file(name, text, out_dir=None):
    """Write atomically and world-readable: CUPS refuses to serve anything else."""
    directory = out_dir or OUT_DIR
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o755)
    path = os.path.join(directory, name)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def user_totals(db, month, default_allowance):
    overrides = {r["user_key"]: r["pages"] for r in db.execute("SELECT * FROM allowances")}
    rows = db.execute(
        "SELECT user_key, pages, ts, user_name FROM jobs WHERE month = ? ORDER BY ts", (month,))
    users = {}
    for r in rows:
        u = users.setdefault(r["user_key"], {"key": r["user_key"], "pages": 0, "jobs": 0})
        u["pages"] += r["pages"]
        u["jobs"] += 1
        u["last"] = r["ts"]
        u["name"] = r["user_name"]
    for u in users.values():
        u["allowance"] = overrides.get(u["key"], default_allowance)
        u["custom"] = u["key"] in overrides
        u["over"] = u["allowance"] > 0 and u["pages"] > u["allowance"]
    return sorted(users.values(), key=lambda u: (-u["pages"], u["name"].casefold()))


def render_month(db, month, months, zone, default_allowance, unreadable, is_index):
    def local(ts, fmt="%b %-d, %-I:%M %p"):
        return datetime.fromtimestamp(ts, zone).strftime(fmt)

    users = user_totals(db, month, default_allowance)
    printers = db.execute(
        "SELECT printer, SUM(pages) AS pages, COUNT(*) AS jobs FROM jobs WHERE month = ?"
        " GROUP BY printer ORDER BY pages DESC, printer", (month,)).fetchall()
    jobs = db.execute("SELECT * FROM jobs WHERE month = ? ORDER BY ts DESC, id DESC", (month,)).fetchall()

    total_pages = sum(u["pages"] for u in users)
    over_count = sum(1 for u in users if u["over"])
    title = month_title(month)

    p = []
    p.append("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">")
    p.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    p.append("<meta name=\"robots\" content=\"noindex\">")
    p.append(f"<title>Print usage: {esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>")
    p.append(f"<h1>Print usage: {esc(title)}</h1>")
    if default_allowance > 0:
        p.append(f"<p class=\"note\">Monthly allowance: {default_allowance:,} pages per person."
                 " Report only: no print job is ever blocked.</p>")
    else:
        p.append("<p class=\"note\">No monthly allowance is set. Report only: no print job is ever blocked.</p>")

    p.append("<div class=\"links\"><a href=\"/status/\">Printer status</a>"
             "<a href=\"/printers/\">Printer list</a><a href=\"/admin\">Administration</a></div>")
    p.append("<nav aria-label=\"Months\"><span>Month:</span>")
    for m in sorted(months, reverse=True):
        here = " class=\"here\" aria-current=\"page\"" if m == month else ""
        p.append(f"<a{here} href=\"{m}.html\">{esc(month_title(m))}</a>")
    p.append("</nav>")
    p.append("<div class=\"links\"><span>Download:</span>")
    p.append(f"<a href=\"{month}-people.csv\" download>Totals by person (CSV)</a>")
    p.append(f"<a href=\"{month}-jobs.csv\" download>All jobs this month (CSV)</a>")
    p.append("</div>")

    p.append("<div class=\"tiles\">")
    for value, label in ((f"{total_pages:,}", "pages"), (f"{len(jobs):,}", "jobs"),
                         (f"{len(users):,}", "people who printed"),
                         (f"{over_count:,}", "over their allowance")):
        p.append(f"<div class=\"tile\"><b>{value}</b><span>{label}</span></div>")
    p.append("</div>")

    p.append("<h2>By person</h2>\n<div class=\"scroll\">")
    if users:
        p.append("<table><thead><tr><th>Person</th><th class=\"num\">Pages</th><th class=\"num\">Jobs</th>"
                 "<th class=\"num\">Allowance</th><th>Used</th><th>Status</th><th>Last job</th></tr></thead><tbody>")
        for u in users:
            if u["allowance"] > 0:
                pct = round(100 * u["pages"] / u["allowance"])
                used = (f"<span class=\"meter\"><i style=\"width:{min(pct, 100)}%\"></i></span>{pct}%")
                allowance = f"{u['allowance']:,}" + (" (custom)" if u["custom"] else "")
                status = "<span class=\"status-over\">Over</span>" if u["over"] else "Within"
            else:
                used, allowance, status = "", "None", ""
            row_class = " class=\"over\"" if u["over"] else ""
            p.append(f"<tr{row_class}><td>{esc(u['name'])}</td><td class=\"num\">{u['pages']:,}</td>"
                     f"<td class=\"num\">{u['jobs']:,}</td><td class=\"num\">{allowance}</td>"
                     f"<td>{used}</td><td>{status}</td><td>{esc(local(u['last']))}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class=\"empty\">No print jobs recorded this month.</div>")
    p.append("</div>")

    p.append("<h2>By printer</h2>\n<div class=\"scroll\">")
    if printers:
        p.append("<table><thead><tr><th>Printer</th><th class=\"num\">Pages</th>"
                 "<th class=\"num\">Jobs</th></tr></thead><tbody>")
        for r in printers:
            p.append(f"<tr><td>{esc(r['printer'])}</td><td class=\"num\">{r['pages']:,}</td>"
                     f"<td class=\"num\">{r['jobs']:,}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class=\"empty\">No print jobs recorded this month.</div>")
    p.append("</div>")

    shown = jobs[:RECENT_JOBS]
    heading = "Jobs" if len(jobs) <= RECENT_JOBS else f"Most recent {RECENT_JOBS} of {len(jobs):,} jobs"
    p.append(f"<h2>{heading}</h2>\n<div class=\"scroll\">")
    if shown:
        p.append("<table><thead><tr><th>Time</th><th>Person</th><th>Printer</th><th class=\"num\">Pages</th>"
                 "<th>Sides</th><th>Color</th><th>Computer</th><th>Document</th></tr></thead><tbody>")
        for r in shown:
            p.append(f"<tr><td>{esc(local(r['ts']))}</td><td>{esc(r['user_name'])}</td><td>{esc(r['printer'])}</td>"
                     f"<td class=\"num\">{r['pages']:,}</td><td>{esc(r['sides'])}</td><td>{esc(r['color'])}</td>"
                     f"<td>{esc(r['host'])}</td><td class=\"doc\">{esc(r['title'])}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class=\"empty\">No print jobs recorded this month.</div>")
    p.append("</div>")

    stamp = datetime.now(zone).strftime("%b %-d, %Y at %-I:%M %p %Z")
    note = f" {unreadable:,} log lines could not be read and are not counted." if unreadable else ""
    p.append(f"<footer>Report generated {esc(stamp)}. Counts come from the print server's own job log.{note}</footer>")
    p.append("</main>\n</body>\n</html>\n")
    page = "\n".join(p)

    out = {f"{month}.html": page}
    if is_index:
        out["index.html"] = page
    out[f"{month}-people.csv"] = csv_text(
        ["person", "pages", "jobs", "allowance", "over_allowance", "last_job"],
        [(u["name"], u["pages"], u["jobs"], u["allowance"] if u["allowance"] > 0 else "",
          "yes" if u["over"] else "no", local(u["last"], "%Y-%m-%d %H:%M")) for u in users])
    out[f"{month}-jobs.csv"] = csv_text(
        ["time", "person", "printer", "pages", "sides", "color", "computer", "document",
         "job_id", "name_as_sent"],
        [(local(r["ts"], "%Y-%m-%d %H:%M:%S"), r["user_name"], r["printer"], r["pages"], r["sides"],
          r["color"], r["host"], r["title"], r["job_id"], r["user_raw"]) for r in reversed(jobs)])
    return out


def render(db, zone, default_allowance, unreadable, only=None, out_dir=None):
    """Render every month, or only the months in `only`. The current month is always included."""
    current = datetime.now(zone).strftime("%Y-%m")
    months = {r[0] for r in db.execute("SELECT DISTINCT month FROM jobs")} | {current}
    targets = months if only is None else (set(only) & months) | {current}
    for month in sorted(targets):
        files = render_month(db, month, months, zone, default_allowance, unreadable, month == current)
        for name, text in files.items():
            write_file(name, text, out_dir)
    return sorted(targets)


# --------------------------------------------------------------- staff page
#
# Served at /status/ to every print subnet without a login, so it holds
# totals only. Document names, computer addresses and the job log stay in the
# report above.

STAFF_CSS = """
:root { color-scheme: light dark; --ink:#14181f; --muted:#5a6472; --bg:#f5f6f8; --panel:#ffffff;
  --line:#dde1e6; --link:#1b5aa0; --up:#177245; --down:#b3261e; --downbg:#fbeceb; --warn:#8a5a00;
  --idle:#8b95a1; --bar:#1b5aa0;
  --sign: Bahnschrift, "DIN Alternate", "Roboto Condensed", "Franklin Gothic Medium", "Arial Narrow", sans-serif;
  --text: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", sans-serif; }
@media (prefers-color-scheme: dark) { :root { --ink:#e8ebef; --muted:#9aa4b1; --bg:#111418;
  --panel:#191d23; --line:#2b323b; --link:#7fb5f2; --up:#4fc48c; --down:#ff8f85; --downbg:#33191a;
  --warn:#e2b04a; --idle:#6f7a87; --bar:#7fb5f2; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--text); }
a { color:var(--link); }
a:focus-visible { outline:2px solid var(--link); outline-offset:2px; border-radius:2px; }
.top { display:flex; flex-wrap:wrap; justify-content:space-between; gap:4px 24px; align-items:baseline;
  max-width:1040px; margin:0 auto; padding:16px 16px 0; }
.top b { font:600 15px/1.4 var(--sign); letter-spacing:.02em; }
.top nav { display:flex; flex-wrap:wrap; gap:4px 18px; font-size:14px; }
main { max-width:1040px; margin:0 auto; padding:0 16px 48px; }
h1 { font:600 clamp(28px, 5vw, 44px)/1.1 var(--sign); margin:28px 0 8px; letter-spacing:-.005em;
  display:flex; align-items:baseline; gap:.4em; }
h2 { font:600 22px/1.2 var(--sign); margin:44px 0 4px; }
h3 { font:600 16px/1.3 var(--sign); margin:24px 0 8px; display:flex; gap:10px; align-items:baseline; }
h3 span { font:400 13px/1.3 var(--text); color:var(--muted); }
p { margin:0 0 8px; max-width:72ch; }
.note { color:var(--muted); }
.note.after { margin-top:10px; }
.notice { border-left:3px solid var(--warn); background:var(--panel); padding:10px 14px; margin:14px 0;
  max-width:72ch; }
.lamp { display:inline-block; width:.62em; height:.62em; border-radius:50%; background:var(--idle);
  flex:none; }
.lamp.up { background:var(--up); }
.lamp.down { background:var(--down); }
.lamp.unchecked { background:transparent; box-shadow:inset 0 0 0 2px var(--idle); }
h1 .lamp { transform:translateY(-.06em); }
.scroll { overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:6px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:9px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
table.board { table-layout:fixed; min-width:600px; }
table.board td { overflow-wrap:anywhere; }
th { font-size:13px; font-weight:600; color:var(--muted); white-space:nowrap; }
td.num, th.num { text-align:right; white-space:nowrap; }
td.name b { font-weight:600; }
td.name span { display:block; color:var(--muted); font-size:13px; }
td.state { white-space:nowrap; }
td.state .lamp { margin-right:8px; }
td.state small { display:block; color:var(--muted); font-size:13px; margin-left:calc(.62em + 8px); }
tr.down td { background:var(--downbg); }
tr.down td:first-child { box-shadow:inset 3px 0 0 var(--down); }
.is-down, .over { color:var(--down); font-weight:600; }
.attn { color:var(--warn); font-weight:600; }
.meter { display:inline-block; width:96px; height:8px; background:var(--line); border-radius:4px;
  vertical-align:middle; margin-right:10px; overflow:hidden; }
.meter i { display:block; height:100%; background:var(--bar); }
tr.over-row .meter i { background:var(--down); }
td.used { white-space:nowrap; }
.months { display:flex; flex-wrap:wrap; gap:4px 16px; margin:8px 0 16px; }
.months span { color:var(--muted); }
.months a.here { color:var(--ink); font-weight:600; text-decoration:none; }
.pair { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:0 24px; }
.empty { color:var(--muted); padding:14px; }
footer { margin-top:40px; color:var(--muted); font-size:13px; }
footer p { max-width:80ch; }
@media (max-width:640px) {
  table.board { min-width:0; display:block; }
  table.board colgroup, table.board thead { display:none; }
  table.board tbody { display:block; }
  table.board tr { display:grid; grid-template-columns:1fr auto; border-bottom:1px solid var(--line); }
  table.board tr:last-child { border-bottom:0; }
  table.board td { border:0; padding:9px 14px 0; }
  table.board td.queue, table.board td.num { padding:2px 14px 10px; font-size:13px; color:var(--muted); }
  table.board td.queue::before { content:"Queue: "; }
  table.board td.num::after { content:" waiting"; }
  tr.down td:first-child { box-shadow:none; }
  table.board tr.down { box-shadow:inset 3px 0 0 var(--down); }
  .meter { width:48px; margin-right:6px; }
  th, td { padding-left:10px; padding-right:10px; }
}
"""

# /usage/ is HTTPS only. From an http page CUPS would redirect to its IP address,
# which doesn't match the certificate, so go straight to https on the same name.
TO_HTTPS = ("if(location.protocol=='http:'){location.href='https://'+location.host+'/usage/';"
            "return false}")

CHECKS = ("up", "down", "unknown", "unchecked")
QUEUE_STATES = ("idle", "printing", "stopped")
OTHER_BUILDING = "Other"


def building_of(printer):
    """'north-library' -> 'north': the part of a queue name before the first hyphen or underscore."""
    m = re.match(r"^([^-_]+)[-_].", printer)
    return m.group(1) if m else ""


def group_by_building(names):
    """Returns [(heading, [name, ...])]. One group with an empty heading if the names don't split."""
    groups, titles = {}, {}
    for name in names:
        key = building_of(name).casefold()
        titles.setdefault(key, building_of(name))
        groups.setdefault(key, []).append(name)
    if len(groups) < 2:
        return [("", sorted(names, key=str.casefold))]
    order = sorted(groups, key=lambda k: (k == "", k))
    return [(titles[k] or OTHER_BUILDING, sorted(groups[k], key=str.casefold)) for k in order]


def load_status(path=None):
    """Read the printer status file written by the CUPS container. None if it isn't usable.

    The file comes from another container, so every field is checked before use.
    """
    def whole(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def text(value):
        return value.strip()[:200] if isinstance(value, str) else ""

    try:
        with open(path or STATUS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("queues"), list):
        return None
    checked_at = whole(data.get("checked_at"))
    if checked_at is None:
        return None
    queues = []
    for q in data["queues"]:
        if not isinstance(q, dict) or not text(q.get("name")):
            continue
        queues.append({
            "name": text(q["name"]), "description": text(q.get("description")),
            "location": text(q.get("location")),
            "state": q.get("state") if q.get("state") in QUEUE_STATES else "idle",
            "accepting": q.get("accepting") is not False,
            "jobs": whole(q.get("jobs")) or 0,
            "check": q.get("check") if q.get("check") in CHECKS else "unchecked",
            "since": whole(q.get("since")),
        })
    return {"checked_at": checked_at, "interval": whole(data.get("interval")) or 300, "queues": queues}


def plural(count, one, many=None):
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def staff_head(title, refresh=None):
    p = ["<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">",
         "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
         "<meta name=\"robots\" content=\"noindex\">"]
    if refresh:
        p.append(f"<meta http-equiv=\"refresh\" content=\"{int(refresh)}\">")
    p.append(f"<title>{esc(title)}</title>\n<style>{STAFF_CSS}</style>\n</head>\n<body>")
    p.append("<header class=\"top\"><b>Print server</b><nav aria-label=\"Print server\">"
             "<a href=\"/status/\">Printer status</a>"
             "<a href=\"/printers/\">Printer list</a>"
             f"<a href=\"/usage/\" onclick=\"{TO_HTTPS}\">Usage detail (sign-in)</a></nav></header>\n<main>")
    return p


def staff_status_section(status, zone, now):
    """The printer board: a one-line answer, then every queue, grouped by building."""
    def local(ts, fmt="%b %-d, %-I:%M %p"):
        return datetime.fromtimestamp(ts, zone).strftime(fmt)

    if status is None:
        return ["<h1><span class=\"lamp\"></span>Printer status isn't available yet</h1>",
                "<p class=\"note\">The server checks its printers a few minutes after it starts."
                " This page updates by itself.</p>"]

    queues = {q["name"]: q for q in status["queues"]}
    checked = [q for q in queues.values() if q["check"] != "unchecked"]
    down = sorted((q["name"] for q in checked if q["check"] == "down"), key=str.casefold)
    pending = sorted((q["name"] for q in checked if q["check"] == "unknown"), key=str.casefold)
    unchecked = len(queues) - len(checked)

    p = []
    if not queues:
        p.append("<h1><span class=\"lamp\"></span>No printers are set up yet</h1>")
    elif not checked:
        p.append(f"<h1><span class=\"lamp unchecked\"></span>{plural(len(queues), 'printer')}, none checked</h1>")
    elif down:
        verb = "is" if len(down) == 1 else "are"
        p.append(f"<h1><span class=\"lamp down\"></span>{len(down):,} of {plural(len(checked), 'printer')}"
                 f" {verb} down</h1>")
    elif pending:
        # Not answering yet, but not confirmed down either: don't claim "all up".
        up = len(checked) - len(pending)
        verb = "is" if up == 1 else "are"
        p.append(f"<h1><span class=\"lamp\"></span>{up:,} of {plural(len(checked), 'printer')} {verb} up</h1>")
    elif len(checked) == 1:
        p.append("<h1><span class=\"lamp up\"></span>The printer is up</h1>")
    else:
        p.append(f"<h1><span class=\"lamp up\"></span>All {len(checked):,} printers are up</h1>")

    minutes = max(1, round(status["interval"] / 60))
    every = "every minute" if minutes == 1 else f"every {minutes} minutes"
    p.append(f"<p class=\"note\">Checked {esc(local(status['checked_at']))}. The server checks {every}.</p>")
    if down:
        p.append(f"<p>Down: {esc(', '.join(down))}.</p>")
    if pending:
        p.append(f"<p>Still checking: {esc(', '.join(pending))}.</p>")
    if now - status["checked_at"] > max(3 * status["interval"], status["interval"] + 180):
        p.append("<p class=\"notice\">The checks have stopped, so this may be out of date."
                 " Let IT know.</p>")
    if not queues:
        return p

    labels = {"up": "Up", "down": "Down", "unknown": "Checking", "unchecked": "Not checked"}
    for heading, names in group_by_building(queues):
        if heading:
            group_down = sum(1 for n in names if queues[n]["check"] == "down")
            tail = f", {group_down} down" if group_down else ""
            p.append(f"<h3>{esc(heading)} <span>{plural(len(names), 'printer')}{tail}</span></h3>")
        p.append("<div class=\"scroll\"><table class=\"board\"><colgroup><col style=\"width:44%\">"
                 "<col style=\"width:24%\"><col style=\"width:19%\"><col style=\"width:13%\"></colgroup>"
                 "<thead><tr><th>Printer</th><th>Printer status</th>"
                 "<th>Queue</th><th class=\"num\">Jobs waiting</th></tr></thead><tbody>")
        for name in names:
            q = queues[name]
            # A class reports its own name as its description; don't repeat it.
            where = ", ".join(x for x in (q["description"], q["location"]) if x and x != name)
            where = f"<span>{esc(where)}</span>" if where else ""
            check = q["check"]
            lamp = check if check in ("up", "down", "unchecked") else ""
            label = labels[check]
            if check == "down":
                label = f"<span class=\"is-down\">{label}</span>"
            since = ""
            if check == "down" and q["since"]:
                since = f"<small>since {esc(local(q['since']))}</small>"
            elif check == "unchecked":
                since = "<small>no network address to test</small>"

            if q["state"] == "stopped":
                queue = "<span class=\"attn\">Paused</span>"
                if not q["accepting"]:
                    queue += ", not accepting jobs"
            elif not q["accepting"]:
                queue = "<span class=\"attn\">Not accepting jobs</span>"
            else:
                queue = "Printing" if q["state"] == "printing" else "Ready"

            row = " class=\"down\"" if check == "down" else ""
            p.append(f"<tr{row}><td class=\"name\"><b>{esc(name)}</b>{where}</td>"
                     f"<td class=\"state\"><span class=\"lamp {lamp}\"></span>{label}{since}</td>"
                     f"<td class=\"queue\">{queue}</td><td class=\"num\">{q['jobs']:,}</td></tr>")
        p.append("</tbody></table></div>")
    if unchecked:
        p.append(f"<p class=\"note after\">{plural(unchecked, 'printer')} can't be"
                 " checked from the server and "
                 f"{'is' if unchecked == 1 else 'are'} left out of the count above.</p>")
    return p


def staff_usage_section(db, month, months, current, default_allowance):
    """Page totals for one month: by person, by printer and by building. Totals only."""
    users = user_totals(db, month, default_allowance)
    printers = db.execute(
        "SELECT printer, SUM(pages) AS pages, COUNT(*) AS jobs FROM jobs WHERE month = ?"
        " GROUP BY printer ORDER BY pages DESC, printer", (month,)).fetchall()

    p = [f"<h2>Pages printed in {esc(month_title(month))}</h2>"]
    if default_allowance > 0:
        p.append(f"<p class=\"note\">Each person's allowance is {default_allowance:,} pages a month."
                 " It is a guide: nothing stops printing when you pass it.</p>")
    if len(months) > 1:
        p.append("<nav class=\"months\" aria-label=\"Months\"><span>Month:</span>")
        for m in sorted(months, reverse=True):
            here = " class=\"here\" aria-current=\"page\"" if m == month else ""
            # Absolute, so the links also work when the page is opened as /status.
            target = "/status/" if m == current else f"/status/{m}.html"
            p.append(f"<a{here} href=\"{target}\">{esc(month_title(m))}</a>")
        p.append("</nav>")

    if not users:
        p.append("<div class=\"scroll\"><div class=\"empty\">Nothing has been printed this month yet.</div></div>")
        return p

    p.append("<h3>By person</h3>\n<div class=\"scroll\"><table><thead><tr><th>Person</th>"
             "<th class=\"num\">Pages</th><th class=\"num\">Jobs</th><th>Allowance used</th>"
             "</tr></thead><tbody>")
    for u in users:
        if u["allowance"] > 0:
            pct = round(100 * u["pages"] / u["allowance"])
            used = f"<span class=\"meter\"><i style=\"width:{min(pct, 100)}%\"></i></span>{pct}%"
            if u["custom"]:
                used += f" of {u['allowance']:,}"
            if u["over"]:
                used += " <span class=\"over\">over</span>"
        else:
            used = ""
        row = " class=\"over-row\"" if u["over"] else ""
        p.append(f"<tr{row}><td>{esc(u['name'])}</td><td class=\"num\">{u['pages']:,}</td>"
                 f"<td class=\"num\">{u['jobs']:,}</td><td class=\"used\">{used}</td></tr>")
    p.append("</tbody></table></div>")

    def totals_table(heading, first, rows):
        out = [f"<div><h3>{heading}</h3>\n<div class=\"scroll\"><table><thead><tr><th>{first}</th>"
               "<th class=\"num\">Pages</th><th class=\"num\">Jobs</th></tr></thead><tbody>"]
        for label, pages, jobs in rows:
            out.append(f"<tr><td>{esc(label)}</td><td class=\"num\">{pages:,}</td>"
                       f"<td class=\"num\">{jobs:,}</td></tr>")
        out.append("</tbody></table></div></div>")
        return out

    p.append("<div class=\"pair\">")
    p += totals_table("By printer", "Printer", [(r["printer"], r["pages"], r["jobs"]) for r in printers])
    by_name = {r["printer"]: r for r in printers}
    groups = group_by_building(by_name)
    if len(groups) > 1:
        rows = [(heading, sum(by_name[n]["pages"] for n in names), sum(by_name[n]["jobs"] for n in names))
                for heading, names in groups]
        p += totals_table("By building", "Building", sorted(rows, key=lambda r: (-r[1], r[0].casefold())))
    p.append("</div>")
    return p


def staff_foot(zone, with_status):
    stamp = datetime.now(zone).strftime("%b %-d, %Y at %-I:%M %p %Z")
    p = [f"<footer><p>Page updated {esc(stamp)}."]
    if with_status:
        p.append("Up means the printer answered on its network connection. It doesn't show paper,"
                 " toner or jams.")
    p.append("Page counts come from the print server's own job log.</p></footer>")
    p.append("</main>\n</body>\n</html>\n")
    return p


def render_staff(db, zone, default_allowance, status, only=None, out_dir=None):
    """Write the staff pages: index.html (status and this month) and one page per earlier month."""
    current = datetime.now(zone).strftime("%Y-%m")
    months = {r[0] for r in db.execute("SELECT DISTINCT month FROM jobs")} | {current}
    targets = months if only is None else (set(only) & months) | {current}
    for month in sorted(targets):
        if month == current:
            p = staff_head("Printer status and usage", refresh=300)
            p += staff_status_section(status, zone, int(time.time()))
            name = "index.html"
        else:
            p = staff_head(f"Print usage: {month_title(month)}")
            name = f"{month}.html"
        p += staff_usage_section(db, month, months, current, default_allowance)
        p += staff_foot(zone, month == current)
        write_file(name, "\n".join(p), out_dir or STAFF_DIR)
    return sorted(targets)


# ------------------------------------------------------------------ service

def snapshot(db, zone, default_allowance, unreadable):
    """Everything besides new jobs that changes what the reports say."""
    allowances = tuple(db.execute("SELECT user_key, pages FROM allowances ORDER BY user_key").fetchall())
    months = tuple(r[0] for r in db.execute("SELECT DISTINCT month FROM jobs ORDER BY month"))
    return (tuple(tuple(a) for a in allowances), months, default_allowance,
            datetime.now(zone).strftime("%Y-%m"), unreadable)


def cycle(state, zone, default_allowance):
    db = open_db()
    try:
        changed, unreadable = ingest(db, zone)
        snap = snapshot(db, zone, default_allowance, unreadable)
        rebuild = state.get("snapshot") != snap
        if rebuild:
            done = render(db, zone, default_allowance, unreadable)
            print(f"reports rebuilt: {', '.join(done)}", flush=True)
        else:
            # The current month is rewritten every cycle, so the "Report generated"
            # time on the page shows that the service is alive.
            render(db, zone, default_allowance, unreadable, only=changed)
            if changed:
                print(f"new jobs recorded in: {', '.join(sorted(changed))}", flush=True)
        state["snapshot"] = snap

        # The staff page is rewritten every cycle too, so it picks up each printer check.
        try:
            render_staff(db, zone, default_allowance, load_status(), only=None if rebuild else changed)
            problem = None
        except OSError as error:
            problem = f"{type(error).__name__}: {error}"
        if problem and problem != state.get("staff_problem"):
            print(f"ERROR: staff page not written: {problem}", file=sys.stderr, flush=True)
        state["staff_problem"] = problem
    finally:
        db.close()
    with open(HEARTBEAT, "w") as handle:
        handle.write(str(int(time.time())))


def run_service():
    interval = max(env_int("USAGE_INTERVAL_SECONDS", 60), 5)
    default_allowance = env_int("USAGE_MONTHLY_PAGES", 0)
    zone = local_zone()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"usage reporting started: allowance={default_allowance or 'none'}, zone={zone.key},"
          f" every {interval}s", flush=True)
    state = {}
    while True:
        try:
            cycle(state, zone, default_allowance)
        except Exception as error:  # keep the service alive; the next cycle retries
            print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        time.sleep(interval)


def run_once():
    cycle({}, local_zone(), env_int("USAGE_MONTHLY_PAGES", 0))


def allowance_command(args):
    db = open_db()
    try:
        if args.action == "list":
            default = env_int("USAGE_MONTHLY_PAGES", 0)
            print(f"default: {default if default else 'none'}")
            for r in db.execute("SELECT user_key, pages FROM allowances ORDER BY user_key"):
                print(f"{r['user_key']}: {r['pages']}")
            return
        if not args.user:
            sys.exit("ERROR: give a user name")
        key, name = normalize_user(args.user)
        if args.action == "set":
            if args.pages is None or args.pages < 0:
                sys.exit("ERROR: give the monthly pages as a whole number, 0 for no allowance")
            db.execute("INSERT INTO allowances (user_key, pages) VALUES (?, ?)"
                       " ON CONFLICT(user_key) DO UPDATE SET pages = excluded.pages", (key, args.pages))
            print(f"{name}: {args.pages} pages per month")
        else:
            db.execute("DELETE FROM allowances WHERE user_key = ?", (key,))
            print(f"{name}: back to the default allowance")
        db.commit()
        print("The report updates within a minute.")
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="usage", description="Print usage reporting for CUPS.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="ingest and render in a loop")
    sub.add_parser("once", help="ingest and render one time")
    allowance = sub.add_parser("allowance", help="per-user monthly allowances")
    allowance.add_argument("action", choices=("list", "set", "clear"))
    allowance.add_argument("user", nargs="?")
    allowance.add_argument("pages", nargs="?", type=int)
    args = parser.parse_args(argv)
    if args.command == "run":
        run_service()
    elif args.command == "once":
        run_once()
    else:
        allowance_command(args)


if __name__ == "__main__":
    main()
