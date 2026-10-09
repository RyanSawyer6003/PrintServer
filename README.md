# PrintServer

Dockerized CUPS print server. The container has two macvlan network legs: one facing clients and admins, and one facing the printer network. They can be two NICs or two VLANs on one NIC (see [Network layouts](#network-layouts)).

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
| Staff page: printer status and pages per printer | `http://<server>:631/status/` | Print subnets, no login |
| Usage reports: totals, job log, CSV | `https://<server>:631/usage/` | Admin subnets, admin or view-only login |
| CUPS administration | `https://<server>:631/admin` | Admin subnets, admin login |

The CUPS pages have **Status** and **Usage** links in their navigation bar.

## Installing printers on clients

See [docs/client-setup.md](docs/client-setup.md): finding queues in the web interface, and installing them on Windows, macOS, Linux and ChromeOS, one at a time or by script.

## Configuration

All site-specific values live in `.env`, which is gitignored. Copy `.env.example` to `.env` and fill it in. **Don't commit real values.**

Every variable `.env` takes, grouped as in `.env.example`. **Required** means the stack won't start without it.

| Variable | Default | What it does |
|---|---|---|
| **Admin account** | | |
| `CUPS_ADMIN_USER` | `printadmin` | Account that signs in to `/admin` and `/usage/`. Created inside the container at start, as a member of the CUPS admin group. |
| `CUPS_ADMIN_PASSWORD` | random | Password for that account, applied on every start. If blank, a random one is generated when the container is created and printed once in `docker logs`. |
| **Containers** | | |
| `CUPS_CONTAINER_NAME` | `cups` | Name of the CUPS container, as used in `docker exec` and `docker logs`. |
| `USAGE_CONTAINER_NAME` | `cups-usage` | Name of the usage reporting container. |
| `CUPS_HOSTNAME` | `CUPS_SERVER_NAME` | The container's hostname, which is the name on CUPS's self-signed certificate. Leave it unset unless it has to differ from the server name. Changing it makes CUPS issue a new certificate, which clients then have to trust again. |
| **Service network** (clients and admins) | | |
| `SERVICES_PARENT_IF` | Required | Host interface the service network uses: a NIC (`eth0`) or a VLAN sub-interface (`eth0.10`). See [Network layouts](#network-layouts). |
| `SERVICES_SUBNET` | Required | The service network, as a CIDR. |
| `SERVICES_GATEWAY` | Required | Its gateway. This is the container's default route. |
| `SERVICES_IP` | Required | The container's address on the service network. `CUPS_SERVER_NAME` must resolve to it. |
| **Printer network** | | |
| `PRINTER_PARENT_IF` | Required | Host interface the printer network uses: a NIC (`eth1`) or a VLAN sub-interface (`eth0.20`). |
| `PRINTER_SUBNET` | Required | The printer network, as a CIDR. |
| `PRINTER_GATEWAY` | Required | Its gateway. Docker needs it to define the network; the container never uses it as its default route. |
| `PRINTER_IP` | Required | The container's address on the printer network. Connections to the printers come from it. |
| **Names** | | |
| `CUPS_SERVER_NAME` | Required | The full DNS name clients print to. CUPS puts it in the printer addresses it hands out, and it is the certificate name unless `CUPS_HOSTNAME` is set. |
| `CUPS_SERVER_ALIASES` | none | Other host names CUPS answers to, space- or comma-separated, such as a reverse proxy's name. CUPS rejects requests for a name that isn't listed. |
| **Who can connect** | | |
| `CUPS_PRINT_SUBNETS` | Required | Client subnets allowed to print, browse the queues, and open the staff page. Leave out any network that shouldn't print. |
| `CUPS_ADMIN_SUBNETS` | Required | Subnets allowed to open `/admin` and `/usage/`. A login and HTTPS are always required as well. Behind a reverse proxy, set it to the proxy's address only (`<proxy IP>/32`). |
| **Staff page** | | |
| `STATUS_CHECK_MINUTES` | `5` | Minutes between checks of whether each printer answers on its print port. |
| `STATUS_CHECK_TIMEOUT` | `3` | Seconds to wait for a printer to accept the connection before the check counts as failed. |
| `STATUS_DOWN_AFTER` | `2` | Failed checks in a row before a printer shows as down. |
| **Usage reports** | | |
| `USAGE_VIEWER_USER` | none | Name of an optional view-only account that can sign in to `/usage/` and nothing else. Set it together with the password. |
| `USAGE_VIEWER_PASSWORD` | none | Password for the view-only account, applied on every start. |
| `USAGE_MONTHLY_PAGES` | `0` | Pages each person may print per month. People over it are flagged on the report; nobody is blocked. `0` means no allowance. |
| `USAGE_TIMEZONE` | `UTC` | Time zone for month boundaries and the times shown on the pages, as an IANA name such as `America/New_York`. |
| `CUPS_DOCROOT` | `/usr/share/cups/doc-root` | Where CUPS serves web pages from inside the container. Set it only if the container warns at startup that the reports are mounted in the wrong place; the warning gives the value. |
| **Backups** (read by `scripts/backup.sh`) | | |
| `RESTIC_REPOSITORY` | none | Where the encrypted snapshots go: an absolute path on the Docker host, or a restic remote such as `s3:...`. Backups don't run until it and the password are set. |
| `RESTIC_PASSWORD` | none | Encrypts the snapshots. Keep a copy somewhere other than this server; without it the backup can't be opened. |
| `BACKUP_KEEP_DAILY` | `14` | Nightly snapshots to keep. |
| `BACKUP_KEEP_WEEKLY` | `8` | Weekly snapshots to keep. |
| `BACKUP_KEEP_MONTHLY` | `12` | Monthly snapshots to keep. |
| `BACKUP_ALLOW_SAME_DISK` | `0` | `1` allows a repository path on the same disk as the Docker volumes. The script refuses one otherwise, because that is what an unmounted backup drive looks like. |
| `BACKUP_IMAGE` | pinned in the script | The restic container image the script runs. Set it to use a different restic version. |
| `BACKUP_STATE_DIR` | `/var/lib/printserver-backup` | Directory on the host where the script keeps its lock and the time of the last successful run. Rarely needs changing. |
| `AWS_*`, `B2_*`, other `RESTIC_*` | none | Storage credentials and restic options for a remote repository, under restic's own names. All are passed to restic. See [docs/backup.md](docs/backup.md). |

When a change takes effect:

- **Most values:** run `docker compose up -d`. Compose recreates the containers with the new values.
- **`SERVICES_*` and `PRINTER_*`:** Docker networks that already exist don't change. Run `docker compose down`, then `docker compose up -d` (see [Changing the layout](#changing-the-layout)).
- **Backup values:** `scripts/backup.sh` reads `.env` each time it runs. Nothing needs restarting.

More detail on a group of settings: [Network layouts](#network-layouts), [Access model](#access-model), [Staff page](#staff-page), [Usage reports](#usage-reports), [Backups](#backups).

Subnet lists are space- or comma-separated IPv4 CIDRs. The container won't start if a list is empty or contains a malformed entry.

`cupsd.conf` is rendered from `cupsd.conf.template` on every start and checked with `cupsd -t` before the scheduler launches. Template changes take effect on restart, even though `/etc/cups` is a persistent volume.

## Network layouts

The container needs two networks:

- **Service network:** where clients and admins reach CUPS. It carries the container's default route. Set with `SERVICES_*`.
- **Printer network:** where the printers are. Set with `PRINTER_*`.

How those two networks reach the Docker host is set in `.env` alone. `docker-compose.yml` is the same for every layout.

### Host connection

| Layout | Switch port(s) | `SERVICES_PARENT_IF` | `PRINTER_PARENT_IF` |
|---|---|---|---|
| **A. Two NICs** | Each NIC is an untagged (access) port on its own network | `eth0` | `eth1` |
| **B. One trunked NIC** | Both VLANs arrive tagged | `eth0.10` | `eth0.20` |
| **C. One NIC, mixed** | Service network untagged (native), printer VLAN tagged | `eth0` | `eth0.20` |

The examples use VLAN 10 for the service network and VLAN 20 for the printer network. `.env.example` shows layout A.

- When a parent is written as `<nic>.<VLAN ID>`, Docker creates the tagged sub-interface itself when it creates the network. Nothing is needed in the host's own network configuration.
- A VLAN that is native (untagged) on the switch port can't also be used as a tagged sub-interface. In layout B, neither VLAN may be the port's native VLAN.
- If the host's own management address is untagged on the same NIC, the port's native VLAN must keep carrying it.
- **If the Docker host is a VM:** each container leg has its own MAC address, so the hypervisor must allow more than one MAC on the virtual NIC (VMware: promiscuous mode and forged transmits on the port group; Hyper-V: MAC address spoofing). For layouts B and C, the virtual NIC must also pass tagged frames.

### Where the service network sits

| Option | Effect |
|---|---|
| **A dedicated server VLAN** | Every client reaches the server through the gateway, so one firewall rule controls all printing. |
| **A client subnet** | Clients on that subnet reach the server directly, without passing the firewall. Other client subnets are routed to it. |

Either way, `SERVICES_SUBNET` and `SERVICES_GATEWAY` describe the network the service leg is on, and `SERVICES_IP` is a free address in it.

### What the network must provide

- A route from every client subnet to `SERVICES_SUBNET`, with 631/tcp allowed to `SERVICES_IP`. With several sites, this applies across the links between them.
- A DNS record for `CUPS_SERVER_NAME` that resolves to `SERVICES_IP` from every client subnet.
- No route for clients into the printer network, and none from the printers back to the LAN. The printers only need to be reached from `PRINTER_IP`.

Don't add the printer subnet to `CUPS_PRINT_SUBNETS` or `CUPS_ADMIN_SUBNETS`. CUPS connects out to the printers; those lists only control who can connect in.

Printers on a subnet the container isn't attached to (another building, for example) are reached through the default route, so that traffic leaves from `SERVICES_IP`. Allow it on your firewall. This path has not been tested.

### Changing the layout

Editing `.env` doesn't change Docker networks that already exist. Recreate them. Leave out `-v` so the volumes (queues, usage history) are kept:

```bash
docker compose down
docker compose up -d
```

Then check on the Docker host:

```bash
# Layouts B and C: each sub-interface exists and shows its VLAN ID
ip -d link show <SERVICES_PARENT_IF>
ip -d link show <PRINTER_PARENT_IF>

# The networks picked up the new parent, subnet, and gateway
docker network inspect <project>_services_vlan \
  --format '{{.Options.parent}} {{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}'

# Container addresses and default route ("default via" must be SERVICES_GATEWAY)
docker exec <container> ip -br addr
docker exec <container> ip route
```

If `SERVICES_IP` changed, update the DNS record for `CUPS_SERVER_NAME`, any firewall rules that name the old address, and a reverse proxy that forwards to it by IP. Clients that print to `CUPS_SERVER_NAME` need no change.

## Access model

- **Printing and status queries:** open to `CUPS_PRINT_SUBNETS`.
- **Staff page:** `/status/` is open to `CUPS_PRINT_SUBNETS` and `CUPS_ADMIN_SUBNETS` without a login. It shows each printer's location, description, queue name and status, and page totals per printer. It never shows who printed, document names, computer addresses or the job log.
- **Other people's jobs:** a job's document name, user name and source computer are visible only to its owner and to admins. Anyone else who lists jobs sees job numbers and states only.
- **Job actions:** cancelling, holding, or moving a job is limited to the job's owner or an admin.
- **Administration:** adding, deleting, or pausing printers requires an authenticated admin, whatever URL the request is sent to. `/admin` also requires TLS.
- **Usage reports:** `/usage/` requires TLS and a login: an admin, or the view-only account. The view-only account can read `/usage/` and nothing else.
- **Server configuration:** `cupsd.conf` can't be changed through the web interface or `cupsctl`; those requests are refused. Change settings in `.env` (or the template) and restart, so the access rules always match what's in the repo.

## Staff page

`http://<server>:631/status/` is for everyone who prints. It needs no login and shows:

- every queue in one list, ordered by location, with its description and queue name
- whether each printer is up or down, and since when it has been down
- the queue state (ready, printing, paused, not accepting jobs) and the number of jobs waiting
- pages and jobs per printer for the month, with a page for each earlier month

It shows nothing about people. Totals per person, the allowance, and the job log are in the usage reports, behind the login.

**Up** means the printer accepted a TCP connection on the port its queue prints to (9100, 631 or 515, read from the queue's address). It doesn't show paper, toner or jams. The check runs inside the CUPS container every `STATUS_CHECK_MINUTES`. By default a printer is reported down after two failed checks in a row, so one missed check doesn't flag it. A queue with no network address (a class, or a USB or discovered queue) shows as "Not checked".

Printer addresses are never written to the page or to the file the check produces.

The page identifies printers by the **description and location** set on each queue, so set both (`lpadmin -p <queue> -D "<description>" -L "<location>"`). That matters most when queue names are codes such as asset numbers. Queues with no location are listed last.

Settings in `.env`. All three are optional whole numbers of 1 or more; the container won't start on anything else.

| Variable | Default | Purpose |
|---|---|---|
| `STATUS_CHECK_MINUTES` | `5` | Minutes between checks |
| `STATUS_CHECK_TIMEOUT` | `3` | Seconds to wait for a printer to accept the connection before the check counts as failed |
| `STATUS_DOWN_AFTER` | `2` | Failed checks in a row before a printer shows as down. `1` flags it on the first miss. |

A printer that stops answering shows as down after `STATUS_DOWN_AFTER` checks, so within about `STATUS_CHECK_MINUTES` × `STATUS_DOWN_AFTER` minutes (10 with the defaults). "Down since" is the time of the first failed check. A printer that answers again shows as up after one check.

After changing a value, run `docker compose up -d`. The startup log confirms what is in effect:

```bash
docker compose logs cups | grep "printer status check started"
```

The page itself is rewritten every minute and reloads in the browser every five minutes; neither is a setting.

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

## Backups

`scripts/backup.sh` saves the queues, the server certificate, the usage history and `.env` as encrypted [restic](https://restic.net) snapshots, on a nightly systemd timer, and restores them onto the same or a rebuilt server. Set `RESTIC_REPOSITORY` and `RESTIC_PASSWORD` in `.env`, then:

```bash
sudo scripts/backup.sh init             # once per backup location
sudo scripts/backup.sh run              # back up now
sudo scripts/backup.sh install-timer    # then every night
scripts/backup.sh status                # exit 0 if a backup succeeded in the last 36 hours
```

See [docs/backup.md](docs/backup.md) for the restore procedure, monitoring, and moving to a different backup location.

## Known PoC limitations

- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on a client subnet.
