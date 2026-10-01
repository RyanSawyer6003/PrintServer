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
| `CUPS_PRINT_SUBNETS` | Client subnets (staff and student, every site) allowed to print and browse queues |
| `CUPS_ADMIN_SUBNETS` | Subnets allowed to open `/admin`. A login is always required. |
| `CUPS_ADMIN_USER` / `CUPS_ADMIN_PASSWORD` | Admin account. The password is applied on every start. |
| `SERVICES_*`, `PRINTER_*`, `CUPS_HOSTNAME` | macvlan interfaces, subnets, gateways, and container IPs |

Subnet lists are space- or comma-separated IPv4 CIDRs. The container won't start if a list is empty or contains a malformed entry.

`cupsd.conf` is rendered from `cupsd.conf.template` on every start and checked with `cupsd -t` before the scheduler launches. Template changes take effect on restart, even though `/etc/cups` is a persistent volume.

## Access model

- **Printing and status queries:** open to `CUPS_PRINT_SUBNETS`.
- **Job actions:** cancelling, holding, or moving a job is limited to the job's owner or an admin.
- **Administration:** adding, deleting, or pausing printers requires an authenticated admin, whatever URL the request is sent to. `/admin` also requires TLS.

## Reverse proxy for the admin UI (optional)

You can put the web UI behind a reverse proxy such as Nginx Proxy Manager under its own hostname, while clients keep printing directly to the container.

1. Set `CUPS_SERVER_NAME` to the container's direct name. This is the name printer URIs use, so jobs never go through the proxy.
2. Add the proxy hostname to `CUPS_SERVER_ALIASES`. CUPS rejects any `Host` header it doesn't recognize.
3. Set `CUPS_ADMIN_SUBNETS` to the proxy's IP as a `/32`. All proxied requests reach CUPS from that address, and direct `/admin` access to the container is then refused.
4. In the proxy, forward with scheme **https** to the container on port 631. `/admin` requires TLS, so plain `http` upstream causes a redirect loop. CUPS's self-signed certificate is fine for this hop.
5. Restrict who can reach the proxy host itself with the proxy's own access list. CUPS only sees the proxy's IP, not the real client's.

If the proxy is down, administer from the host with `docker exec <container> lpadmin ...`.

## Known PoC limitations

- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on a client subnet.
