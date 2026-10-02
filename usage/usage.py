#!/usr/bin/env python3
"""Print usage reporting for a CUPS server.

Reads the CUPS page_log, keeps one row per print job in SQLite, and writes
static HTML and CSV reports that CUPS serves to administrators under /usage/.

Report only: this never blocks or changes a print job. It has no network
access and only reads the log files.

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
        if state.get("snapshot") != snap:
            done = render(db, zone, default_allowance, unreadable)
            print(f"reports rebuilt: {', '.join(done)}", flush=True)
        else:
            # The current month is rewritten every cycle, so the "Report generated"
            # time on the page shows that the service is alive.
            render(db, zone, default_allowance, unreadable, only=changed)
            if changed:
                print(f"new jobs recorded in: {', '.join(sorted(changed))}", flush=True)
        state["snapshot"] = snap
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
