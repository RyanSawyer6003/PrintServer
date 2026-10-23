# Operations runbook

Day-to-day tasks on a running server. For a new server, see [deploy.md](deploy.md); for backups and restores, [backup.md](backup.md); for clients, [client-setup.md](client-setup.md).

Run the commands on the Docker host, from the repository's directory (`cd <repo>`). Docker needs `sudo` unless your account is in the `docker` group. Commands use the Compose service names `cups` and `usage`, so they work whatever the containers are called.

**Contents**

- [The rules](#the-rules)
- [After any change](#after-any-change)
- [Printers](#printers): add, change, rename, pause, remove, jobs
- [People and access](#people-and-access): allowance overrides, view-only login, admin password
- [Restart, redeploy and update](#restart-redeploy-and-update)
- [Subnet lists](#subnet-lists), including moving clients to a new subnet
- [Changing the layout or `SERVICES_IP`](#changing-the-layout-or-services_ip)
- [When the reverse proxy is down](#when-the-reverse-proxy-is-down)
- [Logs and health](#logs-and-health)
- [What never to change](#what-never-to-change)

## The rules

- **Settings live in `.env`.** The server's access rules are rendered from `.env` and `cupsd.conf.template` at every start. The web interface can't change them, and edits to `/etc/cups/cupsd.conf` inside the container are overwritten at the next start.
- **Queues live on the `cups_config` volume.** Queues, the server certificate and its key are there. They survive restarts and updates, and they are in the nightly backup.
- **Queues can be managed in the web interface or from the command line.** Both change the same queues. The command line below works when the web interface can't be reached.
- **After any change to `.env`, run `sudo docker compose up -d`.** Compose recreates what changed. `docker compose restart` does **not** pick up `.env` changes.
- **Keep the checkout clean.** Don't edit tracked files on the server; `git pull` and the update script expect no local changes.

## After any change

Run these after anything that recreates the containers, and after any network change:

```bash
sudo docker compose ps                                              # both containers (healthy)
sudo docker compose exec cups ip route                              # "default via <SERVICES_GATEWAY>"
sudo docker compose exec cups cat /proc/sys/net/ipv4/ip_forward     # 0
```

The default route must be through the service network and forwarding must be `0`; otherwise a printer could reach the LAN through the server. Then, from a client, open `http://<server>:631/status/` and print a test page.

## Printers

### Add a printer

In the web interface: **Administration → Add Printer** (admin login, over `https`). From the host:

```bash
# A printer that supports IPP Everywhere
sudo docker compose exec cups lpadmin -p <queue> -E \
  -v ipp://<printer-ip>/ipp/print -m everywhere \
  -D "<description>" -L "<building, room>"

# Otherwise: raw port 9100 with a driver. Find one with:
#   sudo docker compose exec cups lpinfo -m | grep -i <model>
sudo docker compose exec cups lpadmin -p <queue> -E \
  -v socket://<printer-ip>:9100 -m <driver> \
  -D "<description>" -L "<building, room>"
```

- **Choose the queue name carefully.** It is the name clients see and part of the address they install, and CUPS can't rename a queue (see [Rename a printer](#rename-a-printer)).
- **Always set a description and a location.** The staff page and the usage report list printers by them.
- **The printer must be on and reachable** when it is added with `-m everywhere`, which asks it for its capabilities.

Then:

1. Check `/status/` shows it as up, within `STATUS_CHECK_MINUTES` (default 5).
2. Install it on the clients that need it ([client-setup.md](client-setup.md)): `https://<server>:631/printers/<queue>`.
3. Print a test page from a client and check it appears on `/usage/` under the right person.

### Change a printer

The queue keeps its name, so clients need no change.

```bash
sudo docker compose exec cups lpadmin -p <queue> -D "<new description>" -L "<new location>"
sudo docker compose exec cups lpadmin -p <queue> -v ipp://<new-printer-ip>/ipp/print     # the printer was re-addressed or replaced
sudo docker compose exec cups lpstat -v <queue>                                            # where it sends jobs now
```

A replacement printer of a different model may need the queue's driver changed as well: add `-m everywhere` (or `-m <driver>`) to the `-v` command.

### Rename a printer

CUPS has no rename. Add a new queue under the new name with the same settings, install it on the clients, and remove the old one once nothing prints to it. Each client's installed printer points at the old name, so it must be removed and added again. Usage history stays under the old name.

### Pause, resume, or stop accepting jobs

```bash
sudo docker compose exec cups cupsdisable -r "<reason>" <queue>   # pause printing; jobs wait in the queue
sudo docker compose exec cups cupsenable <queue>                  # resume
sudo docker compose exec cups cupsreject -r "<reason>" <queue>    # refuse new jobs (clients get an error)
sudo docker compose exec cups cupsaccept <queue>                  # accept again
```

The staff page shows the queue's state. A paused queue keeps its jobs; when it resumes, they print.

### Move or cancel jobs

```bash
sudo docker compose exec cups lpstat -o <queue>                       # waiting jobs
sudo docker compose exec cups lpmove <queue> <other-queue>            # move all waiting jobs to another printer
sudo docker compose exec cups cancel <queue>-<job-number>             # cancel one job
sudo docker compose exec cups cancel -a <queue>                       # cancel every job on a queue
```

### Remove a printer

1. Remove it from the clients first, or they keep a printer that fails.
2. Then on the server:

   ```bash
   sudo docker compose exec cups lpadmin -x <queue>
   ```

Its usage history stays in the report, without a location.

## People and access

### Allowance overrides

The default monthly allowance is `USAGE_MONTHLY_PAGES` in `.env`. For one person:

```bash
sudo docker compose exec usage usage allowance set <user> 800    # this person's monthly allowance
sudo docker compose exec usage usage allowance clear <user>      # back to the default
sudo docker compose exec usage usage allowance list              # the default and every override
```

`<user>` is the name as it appears on the report; a `DOMAIN\` prefix and letter case don't matter. An override of `0` means no allowance for that person. Overrides take effect on the next report refresh (within a minute) and are kept in the `usage_data` volume. The allowance is report only: nobody is ever blocked.

To change the default for everyone, edit `USAGE_MONTHLY_PAGES` in `.env` and run `sudo docker compose up -d`.

### View-only login

For helpdesk staff who need the usage report but not administration.

- **Create or change it:** set `USAGE_VIEWER_USER` and `USAGE_VIEWER_PASSWORD` in `.env`, then `sudo docker compose up -d`.
- **Remove it:** blank both values, then `sudo docker compose up -d`. The account loses access at that start.

The account can open `/usage/` (over `https`) and nothing else: not `/admin`, not queues, not other people's jobs. It does see everything in the report, including document names and source computers.

### Admin password

Set `CUPS_ADMIN_PASSWORD` in `.env`, then `sudo docker compose up -d`. It is applied at every start. Store it in your password manager; it is also in the backup, inside `.env`.

## Restart, redeploy and update

| To | Run |
|---|---|
| Restart after a hang, with nothing changed | `sudo docker compose restart` |
| Apply a change to `.env` | `sudo docker compose up -d` |
| Apply a change to the network settings (`SERVICES_*`, `PRINTER_*`) | `sudo docker compose down`, then `sudo docker compose up -d` (see [Changing the layout](#changing-the-layout-or-services_ip)) |
| Update to the newest images for `IMAGE_TAG` | `sudo scripts/update.sh run` |
| Update the repository checkout (scripts, compose file, docs) | `git pull`, then `sudo docker compose up -d` if `docker-compose.yml` changed |
| Roll back to an earlier image | Set `IMAGE_TAG` to the version (`v1.2.0`) or a dated build (`v1.2.0-20261101`), then `sudo docker compose pull --policy always && sudo docker compose up -d` |

Never add `-v` to `docker compose down`: it deletes the volumes, which hold the queues, the certificate and the usage history.

A restart or update interrupts printing for a few seconds. Jobs that clients send meanwhile are retried by the clients. Jobs already waiting on the server are kept.

## Subnet lists

`CUPS_PRINT_SUBNETS` lists the client subnets that may print and open the staff page. `CUPS_ADMIN_SUBNETS` lists where `/admin` and `/usage/` can be opened from. Both are space- or comma-separated CIDRs.

To change either: edit `.env`, run `sudo docker compose up -d`, then test from a client on an added subnet (`Test-NetConnection <server> -Port 631`, then the staff page).

- **Never add the printer subnet** to either list, including inside a wider range. CUPS connects out to the printers; these lists only control who can connect in.
- **List only networks that should print.** Leave out student and guest networks.
- **Firewall rules follow the list:** a subnet added here also needs a route to `SERVICES_SUBNET` and 631/tcp allowed to `SERVICES_IP`.
- **Behind a reverse proxy,** `CUPS_ADMIN_SUBNETS` is the proxy's address only (`<proxy IP>/32`).

### Moving clients to a new subnet

For a client VLAN migration:

1. **Before the first client moves:** add the new subnet to `CUPS_PRINT_SUBNETS` (and to `CUPS_ADMIN_SUBNETS` if admins move too), run `sudo docker compose up -d`, and allow the new subnet through the firewall to `SERVICES_IP` on 631/tcp.
2. **Move the clients.** Their printers need no change: they print to the server's name.
3. **After the last client has moved:** remove the old subnet from the lists and the firewall, and run `sudo docker compose up -d`.

## Changing the layout or `SERVICES_IP`

Editing `.env` doesn't change Docker networks that already exist, so recreate them:

```bash
sudo docker compose down          # never with -v
sudo docker compose up -d
```

Then run the [checks after any change](#after-any-change) and the README's [Changing the layout](../README.md#changing-the-layout) checks, and test from a client on each client subnet.

When `SERVICES_IP` moves, also:

- [ ] Point the DNS record for `CUPS_SERVER_NAME` at the new address
- [ ] Give every client subnet, at every site, a route to the new `SERVICES_SUBNET` and 631/tcp to the new `SERVICES_IP`
- [ ] Update firewall rules that name the old address
- [ ] Update the reverse proxy, if it forwards to the server by IP

Clients print to the server's name, so their printers need no change.

## When the reverse proxy is down

Printing doesn't use the proxy, so only the admin pages are affected. Every administration task in this runbook works from the Docker host with `sudo docker compose exec cups ...`. To list queues and their state:

```bash
sudo docker compose exec cups lpstat -t
```

The usage report is still written to the `usage_reports` volume. If `CUPS_ADMIN_SUBNETS` is locked to the proxy's address, `/admin` and `/usage/` can't be opened directly until the proxy is back, or until `CUPS_ADMIN_SUBNETS` temporarily includes the admin's subnet (then `sudo docker compose up -d`; undo it afterwards).

## Logs and health

```bash
sudo docker compose ps                                          # containers and health
sudo docker compose logs --tail 50 cups                         # startup, settings rejected, the printer check
sudo docker compose logs --tail 50 usage                        # report rebuilds, merged duplicates
sudo docker compose exec cups tail -n 50 /var/log/cups/error_log
sudo docker compose exec cups tail -n 20 /var/log/cups/page_log # finished jobs
sudo docker compose exec cups printer-status once               # check every printer now
sudo scripts/backup.sh status                                   # last backup
sudo scripts/update.sh status                                   # last update, image age
```

For monitoring, `scripts/backup.sh status` and `scripts/update.sh status` exit non-zero when something needs attention.

A printer down on the staff page means it didn't accept a connection on its print port: check it is on, on the network, and has the address the queue uses (`lpstat -v <queue>`).

## What never to change

| Don't | Because |
|---|---|
| Change server settings in the web interface (**Administration → Server**, **Edit Configuration File**) | It's refused. Settings come from `.env` and the template, so the access rules always match the repository. |
| Edit `/etc/cups/cupsd.conf` in the container | It is rewritten from the template at every start. |
| Run `docker compose down -v` | It deletes the volumes: queues, the certificate and its key, the usage history. |
| Change `CUPS_SERVER_NAME` or `CUPS_HOSTNAME` | CUPS issues a new certificate for the new name, and every client then has to trust it again. Clients also print to the server's name. |
| Delete or replace `/etc/cups/ssl/` on the `cups_config` volume | Same as above: a new certificate for every client. |
| Add the printer subnet to `CUPS_PRINT_SUBNETS` or `CUPS_ADMIN_SUBNETS` | It lets printers reach the server's admin and print services. |
| Run `docker compose up --build` on a production server | It builds a local image under the published image's name. The next update replaces it, but until then the server runs untested code. |
| Edit tracked files in the checkout | `git pull` refuses, and the server no longer matches the repository. Settings belong in `.env`. |
