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
| `SERVICES_*`, `PRINTER_*`, `CUPS_HOSTNAME` | macvlan interfaces, subnets, gateways, and container IPs. See [Network layouts](#network-layouts). |

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
