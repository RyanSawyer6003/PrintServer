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

## Reverse proxy

A reverse proxy in front of the web UI is supported. Add its hostname to `CUPS_SERVER_ALIASES`, and forward to the container over **https** on port 631, because `/admin` requires TLS.

## Known PoC limitations

- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on a client subnet.
