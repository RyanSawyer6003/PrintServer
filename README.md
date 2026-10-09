# PrintServer

Dockerized CUPS print server. The container has two macvlan network legs: one facing clients and admins, and one facing the printer network.

## Deploy

```bash
git clone <this repo> && cd <repo>
cp .env.example .env        # fill in real values; .env is gitignored
docker compose up -d --build
docker logs <CUPS_CONTAINER_NAME>
docker ps                   # STATUS should show (healthy) within ~1 minute
```

The admin UI is at `https://<CUPS_SERVER_NAME>:631/admin` and is HTTPS only. Sign in with the `CUPS_ADMIN_USER` account. CUPS uses a self-signed certificate issued to `CUPS_SERVER_NAME`, so expect a browser warning until clients trust it.

| Page | Address | Who |
|---|---|---|
| Staff page: printer status and page totals | `http://<server>:631/status/` | Print subnets, no login |
| Usage reports: totals, job log, CSV | `https://<server>:631/usage/` | Admin subnets, admin or view-only login |
| CUPS administration | `https://<server>:631/admin` | Admin subnets, admin login |

The CUPS pages have **Status** and **Usage** links in their navigation bar.

## Installing printers on clients

See [docs/client-setup.md](docs/client-setup.md): finding queues in the web interface, and installing them on Windows, macOS, Linux and ChromeOS, one at a time or by script.

## Configuration

All site-specific values live in `.env`. See `.env.example`. **Don't commit real values.**

| Variable | Purpose |
|---|---|
| `CUPS_SERVER_NAME` / `CUPS_SERVER_ALIASES` | Direct name used in printer URIs, plus any other accepted hostnames (e.g. a reverse proxy) |
| `CUPS_PRINT_SUBNETS` | Client subnets allowed to print, browse queues, and open the staff page |
| `CUPS_ADMIN_SUBNETS` | Subnets allowed to open `/admin` and `/usage/`. A login is always required. |
| `CUPS_ADMIN_USER` / `CUPS_ADMIN_PASSWORD` | Admin account. The password is applied on every start. |
| `USAGE_VIEWER_USER` / `USAGE_VIEWER_PASSWORD` | Optional view-only account for `/usage/` |
| `STATUS_CHECK_MINUTES` | How often printers are checked for the staff page (default 5) |
| `SERVICES_*`, `PRINTER_*` | macvlan interfaces, subnets, gateways, and container IPs |
| `CUPS_HOSTNAME` | Container hostname and certificate name. Defaults to `CUPS_SERVER_NAME`. |

Subnet lists are space- or comma-separated IPv4 CIDRs. The container won't start if a list is empty or contains a malformed entry.

`cupsd.conf` is rendered from `cupsd.conf.template` on every start and checked with `cupsd -t` before the scheduler launches. Template changes take effect on restart, even though `/etc/cups` is a persistent volume.

## Access model

- **Printing and status queries:** open to `CUPS_PRINT_SUBNETS`.
- **Staff page:** `/status/` is open to `CUPS_PRINT_SUBNETS` and `CUPS_ADMIN_SUBNETS` without a login. It shows queue names, printer status, and page totals per person, printer and building. It never shows document names, computer addresses or the job log.
- **Other people's jobs:** a job's document name, user name and source computer are visible only to its owner and to admins. Anyone else who lists jobs sees job numbers and states only.
- **Job actions:** cancelling, holding, or moving a job is limited to the job's owner or an admin.
- **Administration:** adding, deleting, or pausing printers requires an authenticated admin, whatever URL the request is sent to. `/admin` also requires TLS.
- **Usage reports:** `/usage/` requires TLS and a login: an admin, or the view-only account. The view-only account can read `/usage/` and nothing else.
- **Server configuration:** `cupsd.conf` can't be changed through the web interface or `cupsctl`; those requests are refused. Change settings in `.env` (or the template) and restart, so the access rules always match what's in the repo.

## Staff page

`http://<server>:631/status/` is for everyone who prints. It needs no login and shows:

- every queue, grouped by building, with its description and location
- whether each printer is up or down, and since when it has been down
- the queue state (ready, printing, paused, not accepting jobs) and the number of jobs waiting
- page totals for the month per person against the allowance, per printer, and per building

**Up** means the printer accepted a TCP connection on the port its queue prints to (9100, 631 or 515, read from the queue's address). It doesn't show paper, toner or jams. The check runs inside the CUPS container every `STATUS_CHECK_MINUTES`. A printer is reported down after two failed checks in a row, so one missed check doesn't flag it. A queue with no network address (a class, or a USB or discovered queue) shows as "Not checked".

Printer addresses are never written to the page or to the file the check produces.

**Building** is the part of a queue name before the first hyphen or underscore: `north-library` and `north-room12` are grouped under `north`. Queues without one are listed under "Other". If no queue name has a prefix, the page shows one list.

Everyone on a print subnet can see every person's page totals on this page. Tell staff before you point them at it.

Run a check by hand and print the result:

```bash
docker compose exec cups printer-status once
```

## Usage reports

A second container, `usage`, reads the CUPS job log and writes the staff page above and a usage report that CUPS serves at `https://<server>:631/usage/`. The report requires a login: the admin account, or the view-only account.

### View-only login

Set `USAGE_VIEWER_USER` and `USAGE_VIEWER_PASSWORD` in `.env` and run `docker compose up -d`. That account can sign in to `/usage/`, where it sees everything in the report, including document names and source computers. It can't open `/admin`, change a queue, or act on other people's jobs. Blank both values to remove it.

It is **report only**. No print job is ever blocked or changed. The container has no network access and reads the log files read-only.

The report shows, for each month:

- pages and jobs per person, against the monthly allowance, with people over it flagged
- pages and jobs per printer
- the job log (time, person, printer, pages, sides, color, computer, document name)
- CSV downloads of the totals and the full job log

Settings in `.env`:

| Variable | Purpose |
|---|---|
| `USAGE_MONTHLY_PAGES` | Monthly allowance per person. `0` means no allowance. |
| `USAGE_TIMEZONE` | Time zone for month boundaries and displayed times |
| `USAGE_VIEWER_USER` / `USAGE_VIEWER_PASSWORD` | View-only login for the report |

Give one person a different allowance:

```bash
docker compose exec usage usage allowance set <user> 800
docker compose exec usage usage allowance clear <user>
docker compose exec usage usage allowance list
```

Notes:

- A job appears on the report within a minute of finishing.
- People are grouped by the user name the client sends, without any `DOMAIN\` prefix and ignoring case. CUPS does not verify that name unless you require authentication for printing.
- Page counts are the totals CUPS records when each job finishes. Check them against a printer's own counter before relying on them.
- History is kept in the `usage_data` volume, so it survives CUPS log rotation and rebuilds. Back that volume up.
- Tests: `python3 -m unittest discover -s usage` and `python3 -m unittest discover -s status`

## Routing

Requires Docker Engine 28+ and Compose 2.33.1+ for `gw_priority`. The container's default route must leave through the client-facing network, never the printer network, and the container must not forward between its two networks. Check after every deploy:

```bash
docker exec <container> ip route                              # "default via" must be SERVICES_GATEWAY
docker exec <container> cat /proc/sys/net/ipv4/ip_forward     # must print 0
```

## Reverse proxy

A reverse proxy in front of the web UI is supported. Add its hostname to `CUPS_SERVER_ALIASES`, and forward to the container over **https** on port 631, because `/admin` requires TLS.

## Known PoC limitations

- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on a client subnet.
