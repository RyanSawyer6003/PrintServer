#!/usr/bin/env python3
"""Printer status check for the CUPS container.

Every STATUS_CHECK_MINUTES it asks CUPS for its queues, opens a TCP connection
to the port each queue prints to, and writes the result to a JSON file that
the usage service turns into the staff page.

"Up" means the printer accepted a connection on its print port. It says
nothing about paper, toner or jams; the queue state from CUPS is recorded next
to it. A printer is reported down only after STATUS_DOWN_AFTER failed checks in
a row, so one missed check doesn't flag it.

Printer addresses are read from CUPS, used for the connection, and never
written anywhere: the output holds queue names, descriptions and locations.

Each change is logged as one line ("printer status change: ..."): a printer
going down or coming back up, and CUPS stopping or resuming a queue. If
STATUS_WEBHOOK_URL is set, the changes found in one check are also sent there
as one message, either plain JSON or (STATUS_WEBHOOK_FORMAT=teams) an Adaptive
Card for a Teams Workflows webhook. A failed send is logged and dropped.

Commands:
    printer-status run            check in a loop (the service)
    printer-status once           check one time and print the result
    printer-status test-webhook   send a test message to STATUS_WEBHOOK_URL
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

STATUS_FILE = os.environ.get("STATUS_FILE", "/var/lib/printserver/status/status.json")
WORKERS = 16
WEBHOOK_TIMEOUT = 10
WEBHOOK_FORMATS = ("json", "teams")

# The port a queue prints to when its address doesn't name one.
DEFAULT_PORTS = {"socket": 9100, "ipp": 631, "ipps": 631, "lpd": 515, "http": 80, "https": 443}

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$|^[0-9A-Fa-f:.]+$")


# ----------------------------------------------------------------- settings

def env_int(name, default, minimum=1):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"ERROR: {name} must be a whole number, got {raw!r}")
    if value < minimum:
        sys.exit(f"ERROR: {name} must be at least {minimum}, got {raw!r}")
    return value


# ------------------------------------------------------------ reading CUPS

def lpstat(*args):
    """Run lpstat in the C locale, so the wording this module reads is fixed."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    done = subprocess.run(["lpstat", *args], env=env, capture_output=True, text=True,
                          errors="replace", timeout=30, check=False)
    error = done.stderr.strip()
    if done.returncode != 0 and error and "No destinations added" not in error:
        raise RuntimeError(f"lpstat {' '.join(args)}: {error}")
    return done.stdout


def parse_printers(text):
    """`lpstat -l -p` -> {queue: {state, description, location}}, in CUPS's order."""
    queues, current = {}, None
    for line in text.splitlines():
        if line.startswith("printer "):
            parts = line.split(" ", 2)
            current = None
            if len(parts) < 3 or "/" in parts[1]:
                continue
            rest = parts[2]
            if rest.startswith("disabled"):
                state = "stopped"
            elif rest.startswith("now printing"):
                state = "printing"
            else:
                state = "idle"
            current = queues.setdefault(parts[1], {"state": state, "description": "", "location": ""})
        elif current is not None and line.startswith("\t"):
            label, _, value = line.strip().partition(":")
            if label == "Description":
                current["description"] = value.strip()
            elif label == "Location":
                current["location"] = value.strip()
    return queues


def parse_devices(text):
    """`lpstat -v` -> {queue: device address}."""
    devices = {}
    for line in text.splitlines():
        if not line.startswith("device for "):
            continue
        name, sep, uri = line[len("device for "):].partition(": ")
        if sep and "/" not in name:
            devices[name] = uri.strip()
    return devices


def parse_accepting(text):
    """`lpstat -a` -> {queue: True if it accepts jobs}."""
    accepting = {}
    for line in text.splitlines():
        if line.startswith("\t") or " " not in line:
            continue
        name, rest = line.split(" ", 1)
        if "/" in name:
            continue
        if rest.startswith("accepting requests"):
            accepting[name] = True
        elif rest.startswith("not accepting requests"):
            accepting[name] = False
    return accepting


def parse_jobs(text, names):
    """`lpstat -o` -> {queue: jobs not finished yet}. Lines start with QUEUE-JOBID."""
    counts = {}
    for line in text.splitlines():
        if not line or line[0].isspace():
            continue
        name, dash, job = line.split(None, 1)[0].rpartition("-")
        if dash and job.isdigit() and name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def target_of(uri):
    """Device address -> (host, port) to connect to, or None if there is nothing to check."""
    try:
        parts = urlsplit(uri.strip())
        scheme = parts.scheme.lower()
        if scheme == "hp":
            # HPLIP network queues: hp:/net/<model>?ip=<address> or ?hostname=<name>
            query = parse_qs(parts.query)
            host = (query.get("ip") or query.get("hostname") or [""])[0]
            port = 9100
        elif scheme in DEFAULT_PORTS:
            host = parts.hostname or ""
            port = DEFAULT_PORTS[scheme] if parts.port is None else parts.port
        else:
            return None
    except ValueError:
        return None
    if not host or not HOST_RE.match(host) or not 0 < port < 65536:
        return None
    return host, port


def read_queues():
    """Ask CUPS for every queue. Returns ({queue: info}, {queue: (host, port) or None})."""
    queues = parse_printers(lpstat("-l", "-p"))
    devices = parse_devices(lpstat("-v"))
    accepting = parse_accepting(lpstat("-a"))
    jobs = parse_jobs(lpstat("-o"), set(queues))
    targets = {}
    for name, info in queues.items():
        info["accepting"] = accepting.get(name, True)
        info["jobs"] = jobs.get(name, 0)
        targets[name] = target_of(devices[name]) if name in devices else None
    return queues, targets


# ----------------------------------------------------------------- checking

def port_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_all(targets, timeout):
    """{queue: (host, port) or None} -> {queue: True, False, or None for not checked}."""
    names = [n for n, t in targets.items() if t]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = list(pool.map(lambda n: port_open(*targets[n], timeout), names))
    results = dict.fromkeys(targets)
    results.update(zip(names, answers))
    return results


def merge(previous, queues, results, now, down_after):
    """Combine this check with the last one. Returns the list written to the status file.

    check is one of:
      up         the printer answered
      down       it failed `down_after` checks in a row
      unknown    it has failed, but not often enough yet, and was never seen up
      unchecked  the queue has no network address to connect to
    since is when `check` last changed; for down, the first failed check of the run.
    """
    before = {q.get("name"): q for q in previous if isinstance(q, dict)}
    merged = []
    for name, info in queues.items():
        old = before.get(name, {})
        old_check = old.get("check")
        entry = dict(info, name=name, check="unchecked", since=None,
                     last_up=old.get("last_up"), failures=0, failing_since=None)
        answer = results.get(name)
        if answer is True:
            entry.update(check="up", last_up=now,
                         since=old.get("since") if old_check == "up" else now)
        elif answer is False:
            failures = int(old.get("failures") or 0) + 1
            failing_since = old.get("failing_since") or now
            entry.update(failures=failures, failing_since=failing_since)
            if failures >= down_after:
                entry.update(check="down",
                             since=old.get("since") if old_check == "down" else failing_since)
            elif old_check == "up":
                entry.update(check="up", since=old.get("since"))
            else:
                entry.update(check="unknown")
        merged.append(entry)
    return merged


# ------------------------------------------------------------------ changes

def changes(previous, merged):
    """Compare this check with the last one. Returns a list of change events.

    event is one of:
      down      the printer is now down, and wasn't at the last check
      up        it was down and answers again
      stopped   CUPS has stopped the queue (it was running at the last check)
      resumed   the queue was stopped and runs again

    A printer seen for the first time raises nothing: after a start, or a
    restore that left no status file, the first checks are a baseline, not news.
    It still raises "down" if it is down before it is ever seen up, because by
    then it has been checked STATUS_DOWN_AFTER times. Removed queues raise nothing.
    """
    before = {q.get("name"): q for q in previous if isinstance(q, dict)}
    events = []
    for entry in merged:
        old = before.get(entry["name"])
        if old is None:
            continue
        common = {"queue": entry["name"], "description": entry.get("description", ""),
                  "location": entry.get("location", "")}
        old_check, new_check = old.get("check"), entry["check"]
        if new_check == "down" and old_check != "down":
            events.append(dict(common, event="down", previous=old_check or "unknown",
                               current="down", since=entry.get("since")))
        elif new_check == "up" and old_check == "down":
            events.append(dict(common, event="up", previous="down", current="up",
                               since=entry.get("since")))
        old_state, new_state = old.get("state"), entry.get("state")
        if old_state is None:
            continue
        if new_state == "stopped" and old_state != "stopped":
            events.append(dict(common, event="stopped", previous=old_state, current="stopped",
                               since=None))
        elif old_state == "stopped" and new_state != "stopped":
            events.append(dict(common, event="resumed", previous="stopped", current=new_state,
                               since=None))
    return events


def describe(event):
    """One log line for one event. Holds no printer address."""
    where = ", ".join(x for x in (event["description"], event["location"]) if x)
    what = "queue " if event["event"] in ("stopped", "resumed") else ""
    line = f"printer status change: {event['queue']} {what}{event['previous']} -> {event['current']}"
    return f"{line} ({where})" if where else line


# ------------------------------------------------------------------ webhook

def webhook_settings():
    """(url, format) from the environment, or (None, format) when no webhook is set."""
    url = (os.environ.get("STATUS_WEBHOOK_URL") or "").strip()
    fmt = (os.environ.get("STATUS_WEBHOOK_FORMAT") or "json").strip().lower()
    if fmt not in WEBHOOK_FORMATS:
        sys.exit(f"ERROR: STATUS_WEBHOOK_FORMAT must be one of {', '.join(WEBHOOK_FORMATS)},"
                 f" got {fmt!r}")
    if url and urlsplit(url).scheme.lower() not in ("http", "https"):
        # Don't echo the value: the URL is a secret.
        sys.exit("ERROR: STATUS_WEBHOOK_URL must start with https:// (or http://)")
    return url or None, fmt


def iso(timestamp):
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


EVENT_TITLES = {"down": "Printer down", "up": "Printer back up",
                "stopped": "Queue stopped", "resumed": "Queue resumed"}


def payload(events, checked_at, fmt, server=""):
    """The body to send for one check's events."""
    items = [dict(e, since=iso(e.get("since"))) for e in events]
    if fmt == "json":
        return {"version": 1, "source": "printserver", "server": server,
                "checked_at": iso(checked_at), "events": items}

    count = len(items)
    title = (EVENT_TITLES[items[0]["event"]] + f": {items[0]['queue']}" if count == 1
             else f"{count} printer status changes")
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium",
             "wrap": True}]
    if server:
        body.append({"type": "TextBlock", "text": server, "isSubtle": True, "spacing": "None",
                     "wrap": True})
    for item in items:
        facts = [{"title": "Change", "value": f"{item['previous']} → {item['current']}"}]
        if item["description"]:
            facts.append({"title": "Description", "value": item["description"]})
        if item["location"]:
            facts.append({"title": "Location", "value": item["location"]})
        if item["since"]:
            facts.append({"title": "Since", "value": item["since"]})
        body.append({"type": "TextBlock", "text": f"{EVENT_TITLES[item['event']]}: {item['queue']}",
                     "weight": "Bolder", "separator": True, "wrap": True})
        body.append({"type": "FactSet", "facts": facts})
    card = {"type": "AdaptiveCard", "version": "1.4", "body": body}
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "contentUrl": None, "content": card}]}


def send(url, body, timeout=WEBHOOK_TIMEOUT):
    """POST JSON. Returns None on success, or a short error that never contains the URL."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "printserver-status"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1024)
        return None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except urllib.error.URLError as error:
        return f"{type(error.reason).__name__}: {error.reason}"
    except (OSError, ValueError) as error:
        return type(error).__name__


def report(events, checked_at, webhook=(None, "json"), server=""):
    """Log each event, and send them all as one webhook message if one is set."""
    for event in events:
        print(describe(event), flush=True)
    url, fmt = webhook
    if not events or not url:
        return
    error = send(url, payload(events, checked_at, fmt, server))
    if error:
        print(f"printer status webhook failed ({error}); {len(events)} change(s) not sent",
              file=sys.stderr, flush=True)


# ------------------------------------------------------------------ service

def load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    queues = data.get("queues") if isinstance(data, dict) else None
    return queues if isinstance(queues, list) else []


def save(path, document):
    """Write atomically and world-readable: the usage service reads it as another user."""
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1)
        handle.write("\n")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def cycle(path, interval, timeout, down_after, webhook=(None, "json"), server=""):
    queues, targets = read_queues()
    results = check_all(targets, timeout)
    now = int(time.time())
    previous = load(path)
    merged = merge(previous, queues, results, now, down_after)
    save(path, {"version": 1, "checked_at": now, "interval": interval, "queues": merged})
    # After the save, so a slow or failing webhook never holds back the staff page.
    report(changes(previous, merged), now, webhook, server)
    return merged


def settings():
    interval = env_int("STATUS_CHECK_MINUTES", 5) * 60
    timeout = env_int("STATUS_CHECK_TIMEOUT", 3)
    down_after = env_int("STATUS_DOWN_AFTER", 2)
    return interval, timeout, down_after


def server_name():
    return (os.environ.get("CUPS_SERVER_NAME") or "").strip()


def summary(merged):
    count = {}
    for q in merged:
        count[q["check"]] = count.get(q["check"], 0) + 1
    return ", ".join(f"{count[k]} {k}" for k in ("up", "down", "unknown", "unchecked") if k in count) \
        or "no queues"


def run_service():
    interval, timeout, down_after = settings()
    webhook = webhook_settings()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    notify = f"webhook ({webhook[1]})" if webhook[0] else "no webhook"
    print(f"printer status check started: every {interval // 60} min, timeout {timeout}s,"
          f" down after {down_after} failed checks, {notify}", flush=True)
    last = None
    while True:
        wait = interval
        try:
            now = summary(cycle(STATUS_FILE, interval, timeout, down_after, webhook,
                                server_name()))
            if now != last:
                print(f"printer status: {now}", flush=True)
            last = now
        except Exception as error:  # cupsd may still be starting; try again soon
            print(f"printer status check failed: {type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
            last, wait = None, min(interval, 15)
        time.sleep(wait)


def run_once():
    interval, timeout, down_after = settings()
    # Changes found here are reported too: the service won't see them again.
    for q in cycle(STATUS_FILE, interval, timeout, down_after, webhook_settings(), server_name()):
        print(f"{q['name']}: {q['check']} (queue {q['state']},"
              f" {'accepting' if q['accepting'] else 'not accepting'}, {q['jobs']} waiting)")


def test_webhook():
    """Send one sample message, so the receiving end can be set up and checked."""
    url, fmt = webhook_settings()
    if not url:
        print("STATUS_WEBHOOK_URL is not set", file=sys.stderr)
        return 2
    sample = [{"queue": "test-queue", "description": "Test message from the print server",
               "location": "Not a real printer", "event": "down", "previous": "up",
               "current": "down", "since": int(time.time())}]
    error = send(url, payload(sample, int(time.time()), fmt, server_name()))
    if error:
        print(f"test message failed ({error})", file=sys.stderr)
        return 1
    print(f"test message sent ({fmt})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="printer-status", description="Printer up/down check for CUPS.")
    parser.add_argument("command", choices=("run", "once", "test-webhook"))
    args = parser.parse_args(argv)
    if args.command == "run":
        run_service()
    elif args.command == "once":
        run_once()
    else:
        sys.exit(test_webhook())


if __name__ == "__main__":
    main()
