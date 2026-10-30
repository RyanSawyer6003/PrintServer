# Deploying a new server

From an empty host to a server that prints, with backups and automatic updates. Every value here is a placeholder: use your own. Once it runs, [runbook.md](runbook.md) covers the day-to-day tasks.

The steps, in order:

1. [Plan the values](#1-plan-the-values)
2. [Prepare the network](#2-prepare-the-network)
3. [Install Docker](#3-install-docker)
4. [Get the repository and write `.env`](#4-get-the-repository-and-write-env)
5. [Start the server](#5-start-the-server)
6. [Check it from the host](#6-check-it-from-the-host)
7. [Check it from a client](#7-check-it-from-a-client)
8. [Add the printers](#8-add-the-printers)
9. [Have clients trust the certificate](#9-have-clients-trust-the-certificate)
10. [Install printers on clients](#10-install-printers-on-clients)
11. [Set up backups](#11-set-up-backups)
12. [Set up updates](#12-set-up-updates)
13. [Optional additions](#13-optional-additions)

Run the commands on the server from the repository's directory. Docker needs `sudo` unless your account is in the `docker` group.

## 1. Plan the values

Everything site-specific goes in `.env`. Decide these first, because the network preparation depends on them.

| Setting | Example | Notes |
|---|---|---|
| Layout | B | How the host connects: A = two NICs, B = one NIC with both networks tagged, C = one NIC with the service network untagged and the printer network tagged. See the README, [Network layouts](../README.md#network-layouts). |
| `SERVICES_PARENT_IF` | `eth0.10` | The host interface for the service network: a NIC, or `<nic>.<VLAN ID>` for a tagged VLAN. Docker creates the sub-interface itself. |
| `SERVICES_SUBNET`, `SERVICES_GATEWAY` | `10.10.10.0/24`, `10.10.10.1` | The network clients and admins reach the server on. Its gateway is the container's default route. |
| `SERVICES_IP` | `10.10.10.50` | The server's address. Free, and outside any DHCP scope. |
| `PRINTER_PARENT_IF` | `eth0.20` | The host interface for the printer network. |
| `PRINTER_SUBNET`, `PRINTER_GATEWAY` | `10.20.20.0/24`, `10.20.20.1` | The network the printers are on. |
| `PRINTER_IP` | `10.20.20.10` | The server's address on the printer network. Free, and outside any DHCP scope. |
| `CUPS_SERVER_NAME` | `direct.example.org` | The full DNS name clients print to. It is also the name on the server's certificate, which every client will trust, so choose it once. |
| `CUPS_PRINT_SUBNETS` | `10.30.0.0/16` | Every client subnet that prints, at every site. **Never** include the printer subnet, even inside a wider range. Leave out networks that shouldn't print, such as student or guest networks. |
| `CUPS_ADMIN_SUBNETS` | `10.30.0.0/16` | Where `/admin` and `/usage/` can be opened from. A login and HTTPS are required as well. Behind a reverse proxy, this becomes the proxy's `/32`. |
| `CUPS_ADMIN_PASSWORD` | | Set your own. If it's blank, a random one is printed once in the logs. |
| `USAGE_TIMEZONE` | `America/Chicago` | Month boundaries in the usage reports. |

## 2. Prepare the network

- **Switch port** for the host, to match the layout. In layout B, neither network may be the port's native (untagged) VLAN. If the host's own management address is untagged on the same port, keep it that way.
- **If the host is a VM**, each container network has its own MAC address, so the virtual NIC must accept more than one: VMware needs promiscuous mode and forged transmits on the port group; Hyper-V needs MAC address spoofing. For layouts B and C, the virtual NIC must also pass tagged frames.
- **DNS:** an A record for `CUPS_SERVER_NAME` pointing to `SERVICES_IP`, resolvable from every client subnet.
- **Routing and firewall:** every client subnet, at every site, reaches `SERVICES_IP` on 631/tcp.
- **Printers** can reach the server's `PRINTER_IP`, and the server reaches them on IPP, 631/tcp. That is the only port the server prints on (see [step 8](#8-add-the-printers)).

Isolating the printer network from the LAN comes later, once every client prints through the server (see [13](#13-optional-additions)).

## 3. Install Docker

You need Docker Engine 28 or later and Compose 2.33.1 or later; earlier versions can't pin the container's default route to the service network. On Debian or Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
docker --version
docker compose version
```

## 4. Get the repository and write `.env`

Nothing is built on the server. The images come from GitHub Container Registry; the repository supplies the compose file, the scripts and the documentation.

```bash
git clone https://github.com/RyanSawyer6003/PrintServer.git ~/PrintServer
cd ~/PrintServer
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in the values from step 1. Leave `IMAGE_TAG=prod`. The README's [Configuration](../README.md#configuration) table describes every setting.

## 5. Start the server

```bash
sudo docker compose config --images     # ghcr.io/.../printserver-cups:prod and printserver-usage:prod
sudo docker compose pull --policy always
sudo docker compose up -d
sudo docker compose ps                  # both containers (healthy) within about a minute
sudo docker compose logs cups           # the admin password, if you left it blank
```

If `docker compose config` stops with "set ... in .env", that setting is missing. If the cups container restarts in a loop, `docker compose logs cups` names the setting it rejected.

## 6. Check it from the host

```bash
sudo docker compose exec cups ip -br addr                          # one address on each network
sudo docker compose exec cups ip route                             # "default via <SERVICES_GATEWAY>"
sudo docker compose exec cups cat /proc/sys/net/ipv4/ip_forward    # 0
sudo docker compose logs cups | grep "printer status"              # the printer check is running
```

The default route must be through the service network and forwarding must be `0`: otherwise a printer could reach the LAN through the server. Check both after every change to the network settings.

The host can't reach its own containers' addresses (a property of macvlan), so every network test from here on is run from a client.

## 7. Check it from a client

From a Windows machine on a client subnet:

```powershell
Resolve-DnsName direct.example.org                        # SERVICES_IP
Test-NetConnection direct.example.org -Port 631           # TcpTestSucceeded : True
Start-Process "http://direct.example.org:631/status/"     # the staff page, no login
Start-Process "https://direct.example.org:631/admin"      # a certificate warning for now, then the admin login
```

## 8. Add the printers

**Add each printer as an IPP Everywhere queue, entering its address by hand: its DNS name or IP address.** CUPS then asks the printer what it supports (color or mono, two-sided, trays, paper sizes) and builds the queue from the answer, and clients read those capabilities from the queue. Most network printers from the last ten years support IPP Everywhere.

**The server prints over IPP only.** The image contains no AppSocket (`socket://`, port 9100), LPD, `http://`, USB or other print backends, so the web interface doesn't offer them and CUPS refuses a queue that uses one. A printer without IPP Everywhere support can't be used through this server; leave it on direct printing. A queue on another connection type, from an older install, can't print: the cups container's log names it at every start (`docker compose logs cups`).

**Don't rely on printer discovery for now.** Discovery (**Find New Printers**, and **Discovered Network Printers** under Add Printer) only looks on the printer network and only offers each printer's IPP address, `ipp://<address>/ipp/print`. But it doesn't show that address until you select an entry, and on images built on Debian 13 (the `dev` channel) selecting a discovered entry garbles the Connection field because of a CUPS bug (README, [known limitations](../README.md#known-poc-limitations)). Entering the address by hand avoids both. Find New Printers also hides printers that already have a queue at the same address.

### In the web interface

1. **Administration → Add Printer**, signed in with the admin account.
2. Under **Other Network Printers**, choose **Internet Printing Protocol (ipp)** rather than a discovered printer.
3. **Connection:** `ipp://<printer DNS name or IP>/ipp/print`, for example `ipp://10.20.20.31/ipp/print`. If the printer rejects that path, try `ipp://<printer>/ipp`; the printer's own web page usually lists its IPP address.
4. **Name** (the queue name), **Description** and **Location**. Leave sharing off.
5. **Make:** Generic. **Model:** IPP Everywhere. The printer must be on and reachable now, because CUPS queries it at this step.
6. **Set Default Options:** two-sided (Duplex) and, for color printers, the default color mode.

### From the server

```bash
# IPP Everywhere, address entered by hand
sudo docker compose exec cups lpadmin -p lib-copier -E \
  -v ipp://10.20.20.31/ipp/print -m everywhere \
  -D "Library copier" -L "Building A, Room 112"
sudo docker compose exec cups lpadmin -p lib-copier -o Duplex=DuplexNoTumble    # two-sided by default

sudo docker compose exec cups lpstat -v                            # the queues and their addresses
```

### Check a queue

```bash
sudo docker compose exec cups grep -E '^\*ColorDevice' /etc/cups/ppd/lib-copier.ppd   # False on a mono printer
sudo docker compose exec cups lpoptions -p lib-copier -l | grep -Ei 'duplex|color'     # the choices clients get
```

A mono printer should show `ColorDevice: False`, and a printer with a duplexer a `Duplex` option. If not, the queue wasn't built from the printer: rebuild it in place with the `lpadmin ... -v ipp://... -m everywhere` line above (same queue name, so usage history and client installs are kept), then reinstall it on clients that already have it. Windows reads a printer's capabilities only when it is installed.

**The model name will read `Printer - IPP Everywhere`.** This is a known CUPS bug in the current Debian package, and it is cosmetic: the capabilities above are unaffected. See the README's [known limitations](../README.md#known-poc-limitations).

Notes:

- The queue name (`-p`) is what clients see as the printer's name. Letters, digits, `-` and `_`.
- Always set a description (`-D`) and a location (`-L`). The staff page lists printers by location, and users tell printers apart by them.
- **A DNS name must resolve inside the container:** check with `sudo docker compose exec cups getent hosts <printer name>`. Otherwise use the IP address, and give the printer a DHCP reservation or a static address.
- **Set defaults through the queue's own options** (`-o Duplex=DuplexNoTumble`, `-o ColorModel=Gray`) or **Set Default Options**. Don't use `-o sides-default=...`: it adds a second, conflicting default that clients may ignore.
- IPP Everywhere reads the printer once, when the queue is built. If the printer gains a duplex unit or a tray later, rebuild the queue.
- Within a few minutes, `/status/` shows each printer as up or down.

## 9. Have clients trust the certificate

CUPS uses a self-signed certificate, issued to the container's hostname (`CUPS_SERVER_NAME` unless `CUPS_HOSTNAME` is set). Windows prints over `https` only to a server whose certificate it trusts.

```bash
# CUPS creates the certificate on the first https connection; this makes one if it doesn't exist yet
sudo docker compose exec cups curl -sk -o /dev/null https://localhost:631/
sudo docker compose exec cups ls /etc/cups/ssl/

# Check it before sending it to clients
sudo docker compose exec cups openssl x509 -in /etc/cups/ssl/direct.example.org.crt -noout \
  -subject -ext subjectAltName,basicConstraints -fingerprint -sha1

# Copy it off the server
sudo docker compose cp cups:/etc/cups/ssl/direct.example.org.crt ~/
```

Before trusting it anywhere:

- The subject and alternative names must hold the **full** server name. If they only hold a short name, `CUPS_HOSTNAME` is wrong: fix it in `.env`, run `docker compose up -d`, and create the certificate again.
- `basicConstraints` must say `CA:FALSE`, so the certificate can't vouch for anything but this server.

Then send it to the clients' trusted root store: with Intune, a **Trusted certificate** profile (Windows 10 and later, computer certificate store, Root); otherwise by Group Policy. Record the fingerprint so you can compare it later.

**Keep the certificate stable.** It lives on the `cups_config` volume, survives container updates, and is in the backup. Changing `CUPS_HOSTNAME` or losing that volume creates a new certificate, which every client then has to trust again.

## 10. Install printers on clients

On Windows, install with `Add-Printer -IppURL`, from an elevated PowerShell or as SYSTEM (from an RMM, for example). It is the only Windows method that reads the queue's capabilities, so color, mono and two-sided printing show correctly. The install finishes in the background, and Windows names the printer after the model, so wait for it and rename it to the queue name. The printer is installed for every user of the machine:

```powershell
# Elevated
$queue  = "lib-copier"
$before = (Get-Printer).Name
Add-Printer -IppURL "https://direct.example.org:631/printers/$queue"
do { Start-Sleep 5; $new = Get-Printer | Where-Object { $_.Name -notin $before -and $_.PortName -like "WSD-*" } } until ($new)
Rename-Printer -Name $new.Name -NewName $queue
```

Print a test page, check that Printing Preferences offers two-sided printing (and no color choice on a mono printer), then open `https://direct.example.org:631/usage/` and confirm the job appears under the real user's name.

[client-setup.md](client-setup.md) covers macOS, Linux and ChromeOS, installing several queues by script, and troubleshooting.

## 11. Set up backups

The backup saves the queues, the certificate and its key, the usage history and `.env`. Set it up before anyone depends on the server; the update in step 12 won't run without a recent backup.

1. Mount the backup location on the host: an external drive or a network share.
2. In `.env`, set `RESTIC_REPOSITORY` (for example `/mnt/backup/printserver`) and `RESTIC_PASSWORD` (long and random). Keep a copy of the password in your password manager: `.env` is inside the backup, so without it the backup can't be opened.
3. Then:

```bash
sudo scripts/backup.sh init             # once per backup location
sudo scripts/backup.sh run
sudo scripts/backup.sh check
sudo scripts/backup.sh install-timer    # nightly at 02:15
scripts/backup.sh status                # the next day: OK
```

The restore procedure is in [backup.md](backup.md).

## 12. Set up updates

New images are published whenever a release is published, and when a scheduled rebuild finds a Debian update: nightly for `dev`, Sundays for `prod`, and at least every 28 days for both (README, [Releases and updates](../README.md#releases-and-updates)). `scripts/update.sh` brings the server up to date:

```bash
sudo scripts/update.sh run                     # once now, to see it work
sudo scripts/update.sh install-timer --daily   # every night at 03:30 host time
systemctl list-timers printserver-update.timer
```

A nightly run that finds nothing new pulls, sees the same images, and restarts nothing, so `--daily` costs little and brings a security fix the night after it is published. Without an option the timer runs on the first Saturday of each month instead, and `--weekly` runs it on Mondays.

A run:

- refuses to start without a successful backup in the last 36 hours;
- pulls the images for `IMAGE_TAG` and recreates the containers that changed;
- waits for both containers to be healthy, then checks the default route and that forwarding is off;
- if any check fails, puts the previous images back, checks again, and exits with an error.

A missed run (the host was off) waits for the next scheduled one rather than restarting the server during the day.

**A test or dev server** that follows the dev builds sets `IMAGE_TAG=dev` and installs the timer with `--daily`, which picks up both the weekly build (Monday morning) and the nightly rebuilds. Give it its own `.env`, with its own addresses and server name, so it never answers for the production server. To update by hand at any time, run `sudo scripts/update.sh run`.

**Monitoring:** `sudo scripts/update.sh status` (it reads the images, so it needs Docker access) exits 0 when an update succeeded in the last 40 days and the running image is less than 45 days old, 1 otherwise, 2 if no update has run. With the daily timer, pass a shorter window, such as `status 3`. Channel images are republished at least every 28 days, so one that keeps getting older means the scheduled publish has stopped: GitHub disables scheduled workflows in a public repository after 60 days without commits. Have your monitoring run this and `scripts/backup.sh status`, and alert on a non-zero exit.

**Rolling back by hand:** set `IMAGE_TAG` in `.env` to a previous version (`v1.2.0`) or one exact build (`v1.2.0-20261101`), then `sudo docker compose pull --policy always && sudo docker compose up -d`. Set it back to `prod` once the problem is fixed, or the server stays on that version. The release notes and the workflow summaries on GitHub list the tags.

**The repository on the server** only needs a `git pull` when the compose file, the scripts or the documentation change; release notes say when. Keep the checkout free of local edits, or `git pull` refuses.

## 13. Optional additions

- **View-only login for helpdesk staff:** set `USAGE_VIEWER_USER` and `USAGE_VIEWER_PASSWORD` in `.env`, then `sudo docker compose up -d`. The account can open `/usage/` and nothing else. README, [View-only login](../README.md#view-only-login).
- **Reverse proxy** for the admin pages, with a trusted certificate and no port number: forward to `https://<server>:631`, add the proxy's name to `CUPS_SERVER_ALIASES`, and set `CUPS_ADMIN_SUBNETS` to the proxy's address only. Print jobs still go straight to the server. README, [Reverse proxy](../README.md#reverse-proxy).
- **Isolate the printer network** once every client prints through the server: block the printer network from reaching the LAN, and clients from reaching the printers directly. Allow only the server's `PRINTER_IP` to the printers, and the printers out to the internet for firmware updates. Then turn off IP forwarding on the Docker host itself (`net.ipv4.ip_forward=0`).
- **Logs:** CUPS writes `error_log` and `page_log` on the `cups_logs` volume; ship them to your log server if you have one.

## Checklist

- [ ] Values decided; DNS record, routing and 631/tcp in place
- [ ] Docker Engine 28+ and Compose 2.33.1+
- [ ] `.env` filled in, `chmod 600`
- [ ] Both containers healthy; default route via `SERVICES_GATEWAY`; forwarding `0`
- [ ] Staff page loads from a client; `/admin` accepts the admin login
- [ ] Printers added as IPP Everywhere queues by DNS name or IP (not from discovery), each with a description and a location; mono printers show `ColorDevice: False`; no queue warning in `docker compose logs cups`; all up on `/status/`
- [ ] Certificate checked (full name, `CA:FALSE`), fingerprint recorded, trusted by clients
- [ ] A Windows client installed with `Add-Printer -IppURL` (elevated) shows the right color and two-sided options, prints over `https`, and the job shows on `/usage/` under the right user
- [ ] Backup initialized, checked, and on its nightly timer; password stored away from the server
- [ ] Update run once by hand and on its timer
- [ ] Monitoring runs `scripts/backup.sh status` and `scripts/update.sh status`
