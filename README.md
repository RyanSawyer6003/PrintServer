# PrintServer

Dockerized CUPS print server. The container has two macvlan network legs: one facing clients and admins, and one facing the printer network.

## Deploy

```bash
git clone <this repo> && cd <repo>
cp .env.example .env        # fill in real values; .env is gitignored
docker compose up -d --build
docker logs <CUPS_CONTAINER_NAME>
```

The admin UI is at `https://<SERVICES_IP>:631/admin`. Sign in with the `CUPS_ADMIN_USER` account.

## Configuration

All site-specific values (interfaces, subnets, IPs, hostname, allowed subnet) live in `.env`. See `.env.example`. **Don't commit real values.**

`cupsd.conf` is rendered from `cupsd.conf.template` on every container start. Template changes take effect on restart, even though `/etc/cups` is a persistent volume.

## Known PoC limitations

- The admin user is created only when it doesn't already exist in the container. Changing `CUPS_ADMIN_PASSWORD` requires recreating the container.
- With macvlan, the Docker host can't reach the container's IPs directly. Test from another machine on the client subnet.
