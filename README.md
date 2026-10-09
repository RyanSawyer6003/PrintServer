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

The admin UI is at `https://<SERVICES_IP>:631/admin` and is HTTPS only. Sign in with the `CUPS_ADMIN_USER` account. Until a real certificate is installed, CUPS uses a self-signed one, so expect a browser warning.

## Installing printers on clients

See [docs/client-setup.md](docs/client-setup.md): finding queues in the web interface, and installing them on Windows, macOS, Linux and ChromeOS, one at a time or by script.

## Configuration

All site-specific values live in `.env`. See `.env.example`. **Don't commit real values.**

| Variable | Purpose |
|---|---|
| `CUPS_SERVER_NAME` / `CUPS_SERVER_ALIASES` | Direct name used in printer URIs, plus any other accepted hostnames (e.g. a reverse proxy) |
| `CUPS_PRINT_SUBNETS` | Client subnets allowed to print and browse queues |
| `CUPS_ADMIN_SUBNETS` | Subnets allowed to open `/admin`. A login is always required. |
| `CUPS_ADMIN_USER` / `CUPS_ADMIN_PASSWORD` | Admin account. The password is applied on every start. |
| `SERVICES_*`, `PRINTER_*`, `CUPS_HOSTNAME` | macvlan interfaces, subnets, gateways, and container IPs |

Subnet lists are space- or comma-separated IPv4 CIDRs. The container won't start if a list is empty or contains a malformed entry.

`cupsd.conf` is rendered from `cupsd.conf.template` on every start and checked with `cupsd -t` before the scheduler launches. Template changes take effect on restart, even though `/etc/cups` is a persistent volume.

## Access model

- **Printing and status queries:** open to `CUPS_PRINT_SUBNETS`.
- **Job actions:** cancelling, holding, or moving a job is limited to the job's owner or an admin.
- **Administration:** adding, deleting, or pausing printers requires an authenticated admin, whatever URL the request is sent to. `/admin` also requires TLS.
- **Usage reports:** `/usage/` requires an authenticated admin and TLS, like `/admin`.
- **Server configuration:** `cupsd.conf` can't be changed through the web interface or `cupsctl`; those requests are refused. Change settings in `.env` (or the template) and restart, so the access rules always match what's in the repo.

## Usage reports

A second container, `usage`, reads the CUPS job log and writes a usage report that CUPS serves at `https://<server>/usage/`. The page requires an admin login, the same as `/admin`.

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
- Tests: `python3 -m unittest discover -s usage`

## Routing

Requires Docker Engine 28+ and Compose 2.33.1+ for `gw_priority`. The container's default route must leave through the client-facing network, never the printer network. Check after every deploy:

```bash
docker exec <container> ip route    # "default via" must be SERVICES_GATEWAY
```

## Reverse proxy

A reverse proxy in front of the web UI is supported. Add its hostname to `CUPS_SERVER_ALIASES`, and forward to the container over **https** on port 631, because `/admin` requires TLS.

## Known PoC limitations

- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on a client subnet.
