# PrintServer

Dockerized CUPS print server. The container has two macvlan network legs: one facing clients and admins, and one facing the printer network. They can be two NICs or two VLANs on one NIC (see [Network layouts](#network-layouts)).

## Deploy

```bash
git clone <this repo> && cd <repo>
cp .env.example .env        # fill in real values; .env is gitignored
docker compose pull --policy always   # the published images for IMAGE_TAG (default: prod)
docker compose up -d
docker logs <CUPS_CONTAINER_NAME>
docker ps                   # STATUS should show (healthy) within ~1 minute
```

**For a new server, follow [docs/deploy.md](docs/deploy.md)**: planning the values, network preparation, adding printers, the certificate, backups and updates, with a checklist at the end.

**For day-to-day tasks on a running server, see [docs/runbook.md](docs/runbook.md)**: printers, allowance overrides, the view-only login, restarts and updates, subnet changes, and what never to change.

The images are built, tested and published by GitHub Actions; see [Releases and updates](#releases-and-updates). To build from the checkout instead, use `docker compose up -d --build`.

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

The variables `.env` takes, grouped as in `.env.example`. **Required** means the stack won't start without it.

| Variable | Default | What it does |
|---|---|---|
| **Images** | | |
| `IMAGE_TAG` | `prod` | Which published images to run: the `prod` or `dev` channel, a version such as `v1.2.0`, or one exact build such as `v1.2.0-20261101`. See [Releases and updates](#releases-and-updates). |
| `IMAGE_REPO` | `ghcr.io/ryansawyer6003/printserver` | Where the images come from. The two images are `<IMAGE_REPO>-cups` and `<IMAGE_REPO>-usage`. Change it only to run your own builds. |
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
| `STATUS_WEBHOOK_URL` | none | Where to send a message when a printer's status changes. See [Status notifications](#status-notifications). |
| `STATUS_WEBHOOK_FORMAT` | `json` | `json`, or `teams` for a Teams Workflows webhook. |
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
| `AWS_*`, `B2_*`, other `RESTIC_*` | none | Storage credentials and restic options for a remote repository, under restic's own names. All are passed to restic. See [docs/backup.md](docs/backup.md). |

When a change takes effect:

- **`IMAGE_TAG`, `IMAGE_REPO`:** run `docker compose pull --policy always`, then `docker compose up -d`.
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
- **Printer connections: IPP only.** The image keeps only the `ipp`, `ipps` and `snmp` CUPS backends, so queues can only be `ipp://` or `ipps://`. AppSocket, LPD, `http://`, USB and mDNS queues can't be created. Discovery (`snmp`, configured by `snmp.conf.template`) broadcasts only on the printer network and offers only IPP addresses. At every start, the log warns about any existing queue on another connection type, since it can't print.
- **Usage reports:** `/usage/` requires TLS and a login: an admin, or the view-only account. The view-only account can read `/usage/` and nothing else.
- **Server configuration:** `cupsd.conf` can't be changed through the web interface or `cupsctl`; those requests are refused. Change settings in `.env` (or the template) and restart, so the access rules always match what's in the repo.

## Staff page

`http://<server>:631/status/` is for everyone who prints. It needs no login and shows:

- every queue in one list, ordered by location, with its description and queue name
- whether each printer is up or down, and since when it has been down
- the queue state (ready, printing, paused, not accepting jobs) and the number of jobs waiting
- pages and jobs per printer for the month, with a page for each earlier month

It shows nothing about people. Totals per person, the allowance, and the job log are in the usage reports, behind the login.

**Up** means the printer accepted a TCP connection on the port its queue prints to (631, read from the queue's address). It doesn't show paper, toner or jams. The check runs inside the CUPS container every `STATUS_CHECK_MINUTES`. By default a printer is reported down after two failed checks in a row, so one missed check doesn't flag it. A queue with no network address (a class, or a USB or discovered queue) shows as "Not checked".

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

### Status notifications

Every change the check finds is logged as one line, with no setup:

```
printer status change: <queue> up -> down (<description>, <location>)
printer status change: <queue> down -> up (<description>, <location>)
printer status change: <queue> queue idle -> stopped (<description>, <location>)
```

A printer is "down" only after `STATUS_DOWN_AFTER` failed checks, as on the staff page. "Stopped" means CUPS has disabled the queue, usually after errors sending to the printer, even if the printer still answers. Ship the container log to your log server to alert on these lines.

To also be sent a message, set `STATUS_WEBHOOK_URL` in `.env` and run `docker compose up -d`. The changes found in one check go out as one message, so a switch outage that takes down fifteen printers sends one message, not fifteen. A printer seen for the first time, after a start or a restore, raises nothing.

| `STATUS_WEBHOOK_FORMAT` | Send to |
|---|---|
| `json` (default) | Anything that accepts a JSON POST: n8n, Power Automate, a script. The body is `{"version": 1, "source": "printserver", "server": ..., "checked_at": ..., "events": [...]}`; each event has `queue`, `description`, `location`, `event` (`down`, `up`, `stopped`, `resumed`), `previous`, `current`, and `since` (UTC, for down and up). |
| `teams` | A Teams channel, through a **Workflows** webhook: in the channel, choose **Workflows** and the template "Send webhook alerts to a channel", and paste the URL it gives you. The older Office 365 connector webhooks are being retired by Microsoft. |

Send a test message after setting it up:

```bash
docker compose exec cups printer-status test-webhook
```

- **The URL is a secret.** Anyone who has it can post to the channel. It is kept in `.env`, passed only to the status check, and never logged.
- **Firewall:** the message leaves from `SERVICES_IP` through the service network's gateway. Allow that address out to the receiver, on 443/tcp for Teams.
- **A failed send is logged and dropped.** It is not retried, and it never delays the staff page or printing. The log line gives the reason (`printer status webhook failed (HTTP 400)`), not the URL.
- Messages hold queue names, descriptions, and locations, never printer addresses.

## Look and feel

The staff page, the usage reports and CUPS's own pages (including `/admin` and the steps for adding a printer) share one look: the same colors, fonts, widths and top bar, and a dark mode that follows the device. It comes from three files in `usage/theme/`:

| File | Used by |
|---|---|
| `tokens.css` | Everything: colors, fonts, corner radius, and their dark-mode values |
| `pages.css` | The staff page and usage reports |
| `cups.css` | CUPS's pages. The CUPS image adds `tokens.css` and this file to the end of CUPS's own stylesheet at build time, so CUPS's templates and behavior are unchanged. |

To change a color everywhere, change it in `tokens.css` and rebuild both images. A CUPS update that changes its page markup can leave a CUPS page looking slightly off, but it can't break it: the theme is only CSS. CI checks that the theme is served.

## Usage reports

A second container, `usage`, reads the CUPS job log and writes the staff page above and a usage report that CUPS serves at `https://<server>:631/usage/`. The report requires a login: the admin account, or the view-only account.

### View-only login

Set `USAGE_VIEWER_USER` and `USAGE_VIEWER_PASSWORD` in `.env` and run `docker compose up -d`. That account can sign in to `/usage/`, where it sees everything in the report, including document names and source computers. It can't open `/admin`, change a queue, or act on other people's jobs. Blank both values to remove it.

It is **report only**. No print job is ever blocked or changed. The container has no network access and reads the log files read-only.

The report shows, for each month:

- pages and jobs per person, against the monthly allowance, with people over it flagged
- pages and jobs per printer, with each printer's location and description
- the job log (time, person, printer, pages, sides, color, computer, document name), with each printer named by its description and location
- CSV downloads of the totals by person, the totals by printer, and the full job log

Printers are named by the description and location set on each queue (`lpadmin -p <queue> -D ... -L ...`), as on the staff page. The report remembers them, so a printer removed from the server keeps its name in earlier months, marked "Removed from server" (so does a queue removed before it was ever seen). "Not set" means a queue that is on the server with no description or location: set them with `lpadmin`. On the staff page a removed printer shows "Removed from server" in place of its location.

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

## Releases and updates

The images are built by GitHub Actions and published to GitHub Container Registry as `ghcr.io/ryansawyer6003/printserver-cups` and `ghcr.io/ryansawyer6003/printserver-usage`. They are public and hold no site values: everything site-specific comes from `.env` and the volumes at run time.

### Branches

| Branch | Holds | Releases from it |
|---|---|---|
| `dev` (the default branch) | Work in progress. Pull requests merge here. | Weekly dev builds and hand-made release candidates, to the `dev` channel |
| `main` | Stable code only: `dev` merged in after it was tested on a dev server, plus hotfixes | `vX.Y.Z` releases, to the `prod` channel |

The publish workflow refuses a `vX.Y.Z` release whose commit isn't on `main`.

- **Promote:** open a pull request from `dev` to `main` and merge it with **Create a merge commit**, never squash or rebase. A merge commit keeps the two branches' histories joined; a squash leaves every later promotion conflicting with the last. Then publish `vX.Y.Z` on `main` ([Publishing a release](#publishing-a-release)).
- **Hotfix:** branch from `main`, merge the fix into `main` by pull request, and publish `vX.Y.Z` from `main` straight away. Then merge `main` into `dev` (a pull request from `main` to `dev`), or the next promotion brings the old code back over the fix. The next dev build moves past the hotfix's version, so `dev` never sorts below `prod`.

### Channels

A server follows a channel through `IMAGE_TAG` in `.env`. The channel is set by the release's tag name:

| Release tag | GitHub release | Channel |
|---|---|---|
| `vX.Y.Z`, e.g. `v1.2.0` | Full release | `prod`: the newest of these |
| `vX.Y.Z-<suffix>`, e.g. `v1.3.0-rc.1` | Pre-release (tick "Set as a pre-release") | `dev`: the newest release of either kind |
| `vX.Y.Z-dev.YYYYMMDD`, e.g. `v1.0.2-dev.20261026` | Pre-release, created weekly from `dev` by the Dev release workflow | `dev` |

The publish workflow refuses a release whose pre-release box doesn't match its name, and tags that fit neither pattern (such as the old `v0.1`).

**Weekly dev builds.** Every Monday the Dev release workflow tags the head of `dev` as a pre-release and publishes it to `dev`, unless nothing has been merged since the newest release. Its notes list the pull requests merged since then. The version is chosen so the build sorts after every existing release: it keeps the version of the previous dev build, or moves to the next patch after anything else (`v1.0.1` → `v1.0.2-dev.<date>`). A release or release candidate made by hand for that version (`v1.0.2-rc.1`, `v1.0.2`) sorts after its dev builds. Prod is never changed by a dev build: promoting is still publishing `vX.Y.Z` by hand.

**Rebuilds.** The current `dev` release is rebuilt every night and the current `prod` release every Sunday, from scratch on a fresh Debian base. A rebuild is published only if it differs from the image the channel serves now: a different commit or any Debian package version changed (`scripts/ci/image-changed.sh`). An image is also republished once it is 28 days old, so a channel image is never older than that. A security fix in Debian reaches `dev` the next night and `prod` by the next Sunday, and on nights with no change nothing is pushed, so servers restart nothing.

Every image also gets two version tags: `v1.2.0`, which moves with each published rebuild of that release, and `v1.2.0-YYYYMMDD`, which never moves. Pin `IMAGE_TAG` to either to stop following a channel. The nightly `dev` rebuild never moves a `prod` release's tags, even when both channels are on the same version.

### What the workflows do

| Workflow | When | What |
|---|---|---|
| **CI** (`.github/workflows/ci.yml`) | Every pull request, and every push to `dev` or `main` | Scans files, commit messages and commit identities for site values (`scripts/ci/check-site-values.sh`); runs shellcheck; builds both images and smoke-tests them (`scripts/ci/smoke-test.sh`). Publishes nothing. |
| **Publish images** (`.github/workflows/publish.yml`) | A release is published, or a dev build is tagged; nightly 06:23 UTC (`dev`); Sundays 07:43 UTC (`prod`); by hand | For a release or dev build: builds and publishes that one. Scheduled: rebuilds the channel's current release and publishes it only if it changed. By hand: rebuilds the channels chosen, with or without that check. Always from scratch on a fresh Debian base, smoke-testing both images, with the release's own test, before pushing either. A failed build or test leaves the channel tags where they were. |
| **Dev release** (`.github/workflows/dev-release.yml`) | Mondays 05:17 UTC; by hand | Tags the head of `dev` as `vX.Y.Z-dev.YYYYMMDD` (`scripts/ci/next-dev-tag.sh`), creates the pre-release with generated notes, and runs the publish workflow for it. Skips weeks with nothing merged. |

The scheduled rebuilds are the security updates: they pick up Debian's fixes without a new release. Scheduled runs use the workflow files on the default branch (`dev`), but each release is built and tested from its own commit. GitHub disables scheduled workflows in a public repository after 60 days with no commits; if that happens, re-enable them under **Actions**, or run them by hand. `scripts/update.sh status` on a server notices: its channel image stops getting younger than 28 days.

The smoke test runs the real entrypoint (so `cupsd.conf` is rendered and checked with `cupsd -t`), checks the Status and Usage links in the CUPS pages, that `/admin` refuses plain http and asks for a login over https, that the container doesn't forward, and runs the unit tests on the image's own Python. It does not test macvlan networking, printing, or the volumes.

### Site-value scan

The scan fails on private addresses outside the placeholder ranges `10.10`, `10.20`, `10.30` and `10.40`, and on domain names other than `example.org` and the public names in `scripts/ci/allowed-domains.txt`. For the values that can't be told apart by pattern (server names, NIC names), add a repository secret **`SITE_DENYLIST`** (Settings → Secrets and variables → Actions) holding them, separated by commas or spaces. The list stays out of the repository, and GitHub masks it in logs. Run the scan locally with `scripts/ci/check-site-values.sh` (add a range such as `origin/main..HEAD` to check commits too).

### Publishing a release

1. For `prod`: promote `dev` to `main` (or merge a hotfix into `main`) and wait for CI to pass. For a release candidate: merge to `dev`.
2. On GitHub: **Releases → Draft a new release**, create the tag on `main` for `v1.2.0`, or on `dev` for `v1.3.0-rc.1` with **Set as a pre-release** ticked, and **Publish release**.
3. **Actions → Publish images** shows the run. Its summary lists every tag pushed and the image digests.

### Updating a server

`scripts/update.sh` pulls the images for `IMAGE_TAG`, recreates what changed, checks that both containers are healthy, that the default route is on the service network and that forwarding is off, and puts the previous images back if a check fails. It refuses to run without a backup from the last 36 hours.

```bash
sudo scripts/update.sh run              # update now
sudo scripts/update.sh install-timer --daily   # every night at 03:30; or no option for the first Saturday of each month
sudo scripts/update.sh status           # for monitoring: exit 0 if current, 1 if not, 2 if never run
```

See [docs/deploy.md](docs/deploy.md#12-set-up-updates). The repository checkout on the server only needs updating when `docker-compose.yml`, the scripts or the docs change; the images don't depend on it.

**Rolling back:** set `IMAGE_TAG` to the previous version or a dated tag (the release page and the workflow summaries list them), then `docker compose pull --policy always && docker compose up -d`. Volumes are untouched by an image change.

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
- **IPP Everywhere queues show the model as `Printer - IPP Everywhere`.** This is an upstream CUPS bug, not a problem with the printers or this repository. The CVE-2024-47175 hardening (upstream CUPS 2.4.11) cleans up the make and model a printer reports, but a loop meant to strip trailing spaces cuts the string at every space: `TOSHIBA e-STUDIO6518A` becomes `TOSHIBA`, and with no model left CUPS falls back to `Printer`. Upstream fixed it in CUPS 2.4.12. Debian backported the hardening into both of the CUPS packages this image is built on (2.4.2 in Debian 12 for `prod`, 2.4.10 in Debian 13 for `dev`), but not the fix yet. It is cosmetic: color, two-sided and the other capabilities are read separately and are correct, and Windows clients are renamed to the queue name at install. Nothing in this repository works around it; it goes away when the image's CUPS package includes the fix.
- **Selecting a discovered printer garbles the Connection field on Debian 13 images** (the `dev` channel). In CUPS 2.4.10 the Add Printer page reads the selected device with `cgiGetTextfield()`, which returns the variable's own storage, then stores the URI back over it, so it copies from freed memory. Upstream CUPS fixed it in 2.4.12 (back to `cgiGetVariable()`, which returns a copy); Debian 13's package doesn't have the fix yet. Debian 12 images (`prod`) aren't affected. Either way, add printers by their DNS name or IP address, entered by hand ([deploy.md, step 8](docs/deploy.md#8-add-the-printers)): discovered entries don't show their address until selected.
