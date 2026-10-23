# Client setup: finding and installing printers

For admins and helpdesk staff. It covers the steps after a queue has been added to the server: how to find it, how to install it on Windows, macOS, Linux and ChromeOS, and how to script the install.

Clients can't discover the server on their own. Browsing (mDNS/DNS-SD) is off, and multicast wouldn't cross subnets or sites anyway. Every install is done by someone who knows the server name and the queue name.

Each method is marked **Verified** (tested against this server) or **Not yet verified** (expected to work, but untested here). Menu names in the operating systems change between versions.

## What you need

| Item | Where it comes from |
|---|---|
| `<server>` | The server's DNS name, the value of `CUPS_SERVER_NAME` in `.env`. Use this name, not an IP address and not a reverse-proxy name: CUPS rejects host names it doesn't know, and print jobs should go straight to the server. |
| `<queue>` | The queue name as shown on the server's Printers page |
| Network access | The client's subnet must be in `CUPS_PRINT_SUBNETS`, and TCP 631 must be open from the client to the server |

A queue can be addressed in four ways. All four point at the same queue:

| Form | Used by |
|---|---|
| `https://<server>:631/printers/<queue>` | Windows, with `Add-Printer -IppURL` (the method below). Encrypted. The device must trust the server's certificate. |
| `http://<server>:631/printers/<queue>` | Not recommended. Unencrypted: print data crosses the network in the clear. |
| `ipp://<server>:631/printers/<queue>` | macOS, Linux, ChromeOS. Unencrypted. |
| `ipps://<server>:631/printers/<queue>` | macOS, Linux, ChromeOS, encrypted. The device must trust the server's certificate. |

The server's certificate is self-signed by CUPS unless you have replaced it. [deploy.md, step 9](deploy.md#9-have-clients-trust-the-certificate) shows how to check it and copy it off the server.

## Find printers on the server

### Web interface

Open `http://<server>:631/printers/` in a browser on a client subnet. No login is needed to view it. **Verified**

- The **Printers** tab lists every queue with its description, location, model and status. Click a queue to open its page; that page's address is the queue's install URL.
- The **Jobs** tab shows queued and completed jobs.
- The **Status** tab opens the staff page (`/status/`): every queue by location with its printer up or down, the queue state, and pages per printer. No login is needed.
- The **Usage** tab opens the usage reports (`/usage/`). It needs HTTPS and the admin or view-only login.
- On a queue's page, **Maintenance → Print Test Page** sends a test page. It may ask for the admin login.
- **Administration** (`https://<server>:631/admin`) needs the admin login and HTTPS. With the default self-signed certificate, expect a browser warning.

### Command line

From a macOS or Linux machine on a client subnet: **Not yet verified**

```bash
lpstat -h <server>:631 -p     # queues and their state
lpstat -h <server>:631 -a     # queues accepting jobs
```

On the Docker host: **Verified**

```bash
docker compose exec cups lpstat -v    # every queue and the printer it sends to
```

### Make queues easy to identify

People only see the queue name, description and location, so set the description and location on every queue:

```bash
docker compose exec cups lpadmin -p <queue> -D "<description>" -L "<building, room>"
```

Choose queue names people can recognise, such as a room or area. The Windows method below names the printer after the queue.

## Windows

Install every printer with **`Add-Printer -IppURL`** over `https`, **from an elevated PowerShell or as SYSTEM** (an RMM or Intune). It is the only Windows method that reads the queue's capabilities from the server: whether the printer is color or mono, two-sided printing, trays and paper sizes.

Those capabilities come from the queue, so the queue must be an IPP Everywhere queue built from the printer ([deploy.md, step 8](deploy.md#8-add-the-printers)). A queue added from printer discovery, or over AppSocket, LPD or Raw, shows up on Windows as a color printer with no two-sided option, whatever the printer really is.

### Trust the server's certificate

Do this once per device, before adding printers. The certificate must go in the **computer's** trusted root store (`LocalMachine\Root`), not the user's: the printer is installed for the whole device, and the print spooler that connects to the server runs as SYSTEM.

- **Managed devices:** an Intune **Trusted certificate** profile (Windows 10 and later; destination store: computer certificate store - Root), or a Group Policy that adds it to *Trusted Root Certification Authorities*. **Verified** with Intune.
- **One device by hand,** from an elevated PowerShell. **Verified.**

  ```powershell
  Import-Certificate -FilePath .\<server>.crt -CertStoreLocation Cert:\LocalMachine\Root
  ```

Compare the certificate's thumbprint with the server's copy before trusting it, and check that the device has it:

```powershell
Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -eq "CN=<server>" |
    Format-List Subject, Thumbprint, NotAfter
```

The certificate must name the full server name (`<server>`), not a short host name: Windows rejects a name mismatch even for a trusted certificate. If the server's certificate is ever replaced, every device needs the new one.

### Quickest: one printer

PowerShell, elevated or as SYSTEM, on a device that trusts the certificate. **Verified** from an elevated session and from an RMM script.

```powershell
# Elevated
$queue  = "<queue>"
$before = (Get-Printer).Name
Add-Printer -IppURL "https://<server>:631/printers/$queue"

# The install finishes in the background, usually within a minute or two
do { Start-Sleep 5; $new = Get-Printer | Where-Object { $_.Name -notin $before -and $_.PortName -like "WSD-*" } } until ($new)

# Windows names the printer after its model; give it the queue's name
Rename-Printer -Name $new.Name -NewName $queue
Get-PrintConfiguration -PrinterName $queue | Format-List Color, DuplexingMode
```

Then:

1. Open **Printing Preferences**: a mono printer offers no color choice, and two-sided printing is offered where the printer has a duplexer.
2. Print a test page, and check that the job appears in the usage report under the right person.

### Things to know

- **Run it elevated.** `-IppURL` needs administrator rights and doesn't work for a standard user. Deploy from an RMM or Intune running as SYSTEM, or from an elevated session.
- **The printer is installed for the whole device.** Every user of that computer sees it. Jobs are still recorded under the name of the person printing.
- **The port is named `WSD-<GUID>`.** It doesn't contain the server or queue name, so a printer can't be traced back to its queue from the port. Name each printer after its queue so the name is the link.
- **Rename after every install.** Windows names the printer after the model the queue reports, which is currently `Printer - IPP Everywhere` for every queue because of a CUPS bug (README, [known limitations](../README.md#known-poc-limitations)). Without the rename, every printer from the server would have that name, told apart only by a number.
- **Capabilities are read once, at install.** After a queue is rebuilt or changed on the server, remove the printer and install it again.
- **Don't use `Add-Printer -PortName "https://..."`** or **Settings → Add device → Select a shared printer by name**. Both create a legacy *Internet Port*, which sends jobs but never reads the queue's capabilities: every printer shows as color, one-sided, with no two-sided option. Printers installed that way should be removed and installed again with `-IppURL`.
- **`https://` fails if the certificate isn't trusted, or doesn't carry the full server name.** The error doesn't say why; check the certificate first (above, and [Troubleshooting](#troubleshooting)).
- **LPR/LPD, raw port 9100, and Standard TCP/IP ports don't work.** The server only accepts IPP on port 631.

### Check and remove

```powershell
# Printers installed the recommended way (IPP class driver on a WSD port).
# This also lists IPP printers added directly rather than through the server.
Get-Printer | Where-Object { $_.PortName -like "WSD-*" -and $_.DriverName -eq "Microsoft IPP Class Driver" } |
    Format-Table Name, PortName -AutoSize

# Older installs on an Internet Port, to remove and install again with -IppURL
Get-Printer | Where-Object PortName -like "*://<server>:631/printers/*" |
    Format-Table Name, PortName -AutoSize

# Remove one
Remove-Printer -Name "<queue>"
```

Printers on a bare IP address or a Standard TCP/IP port do not go through the server, and their jobs won't appear in the usage reports.

### Script: several printers

Install script for any deployment tool that runs as SYSTEM (Intune, an RMM). It can be run any number of times: it adds missing queues, one at a time so each new printer can be matched to its queue, and renames each to its queue name. It first checks that the device trusts the server's certificate, and stops without adding anything if not. A printer with the queue's name on the server's own Internet Port (an older install) is replaced; a printer with that name on any other port, such as a direct-IP printer, is reported and left alone. **Not yet verified as written;** the single-printer version it is based on is verified from an RMM.

```powershell
# Install-PrintQueues.ps1
$Server     = "<server>"
$Thumbprint = "<certificate thumbprint>" # the server certificate's SHA-1 thumbprint, no spaces
$Queues     = @("<queue1>", "<queue2>")
$Driver     = "Microsoft IPP Class Driver"
$TimeoutSec = 300                        # the install finishes in the background

if (-not (Test-Path "Cert:\LocalMachine\Root\$Thumbprint")) {
    Write-Output "The server's certificate isn't in this device's trusted root store yet; nothing added."
    exit 1
}

$problems = @()
foreach ($q in $Queues) {
    $url      = "https://${Server}:631/printers/$q"
    $existing = Get-Printer -Name $q -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.PortName -like "WSD-*" -and $existing.DriverName -eq $Driver) { continue }
        if ($existing.PortName -in @($url, "http://${Server}:631/printers/$q")) {
            # An older Internet Port install of this queue: no capabilities, so reinstall it
            try   { Remove-Printer -Name $q -ErrorAction Stop }
            catch { $problems += "$q : couldn't remove the old Internet Port install: $($_.Exception.Message)"; continue }
        }
        else {
            $problems += "$q : name already used by a printer on port $($existing.PortName)"
            continue
        }
    }

    $before = @(Get-Printer | ForEach-Object Name)
    try   { Add-Printer -IppURL $url -ErrorAction Stop }
    catch { $problems += "$q : $($_.Exception.Message)"; continue }

    $new      = @()
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        Start-Sleep -Seconds 5
        $new = @(Get-Printer | Where-Object {
            $_.Name -notin $before -and $_.PortName -like "WSD-*" -and $_.DriverName -eq $Driver
        })
    } while ($new.Count -eq 0 -and (Get-Date) -lt $deadline)

    if ($new.Count -ne 1) {
        $problems += "$q : expected one new printer within $TimeoutSec seconds, found $($new.Count). If the device already has this queue under another name, remove it and run again."
        continue
    }
    try   { if ($new[0].Name -ne $q) { Rename-Printer -Name $new[0].Name -NewName $q -ErrorAction Stop }; Write-Output "Added $q" }
    catch { $problems += "$q : installed as '$($new[0].Name)' but the rename failed: $($_.Exception.Message)" }
}

if ($problems) { Write-Output ($problems -join "`n"); exit 1 }
exit 0
```

Don't run two copies on one device at the same time: each would see the other's new printer and stop without renaming.

For a deployment tool that runs a detection script first (such as Intune Remediations), use this with the same variables. It checks names only, because the port doesn't identify the queue:

```powershell
# Detect-PrintQueues.ps1 - exit 0 when the certificate and every queue are in place, 1 otherwise
$Thumbprint = "<certificate thumbprint>"
$Queues     = @("<queue1>", "<queue2>")

if (-not (Test-Path "Cert:\LocalMachine\Root\$Thumbprint")) {
    Write-Output "Certificate not trusted yet"; exit 1
}
$missing = $Queues | Where-Object {
    $p = Get-Printer -Name $_ -ErrorAction SilentlyContinue
    -not $p -or $p.PortName -notlike "WSD-*" -or $p.DriverName -ne "Microsoft IPP Class Driver"
}
if ($missing) { Write-Output "Missing or on an old port: $($missing -join ', ')"; exit 1 }
exit 0
```

## macOS

**Not yet verified.**

### Quickest: one printer

1. **System Settings → Printers & Scanners → Add Printer, Scanner, or Fax**, then the **IP** tab (globe icon).
2. **Address:** `<server>` · **Protocol:** Internet Printing Protocol - IPP · **Queue:** `printers/<queue>`
3. **Name:** `<queue>` (or something friendlier) · **Use:** AirPrint if offered, otherwise Generic PostScript Printer
4. Click **Add**. macOS may ask for an administrator's password.

Or in Terminal:

```bash
sudo lpadmin -p <queue> -E -v ipp://<server>:631/printers/<queue> -m everywhere -D "<description>" -L "<location>"
```

`-m everywhere` asks the server for the queue's capabilities, so no vendor driver is needed. If it fails, replace it with the generic PostScript description:
`-P /System/Library/Frameworks/ApplicationServices.framework/Versions/A/Frameworks/PrintCore.framework/Resources/Generic.ppd`

### Check and remove

```bash
lpstat -v                    # each local printer and where it sends jobs
lpoptions -d <queue>         # make it the default
sudo lpadmin -x <queue>      # remove
```

### Script: several printers

For an MDM shell script (these usually run as root) or for running by hand with `sudo`:

```bash
#!/bin/bash
# install-print-queues.sh
SERVER="<server>"
QUEUES=("<queue1>" "<queue2>")

for q in "${QUEUES[@]}"; do
    if lpstat -v "$q" >/dev/null 2>&1; then
        echo "$q already installed"
        continue
    fi
    if lpadmin -p "$q" -E -v "ipp://${SERVER}:631/printers/${q}" -m everywhere; then
        echo "Added $q"
    else
        echo "Failed to add $q" >&2
        status=1
    fi
done
exit ${status:-0}
```

## Linux

**Not yet verified.** The client needs CUPS installed (the `cups` package on most distributions).

### Quickest: one printer

```bash
sudo lpadmin -p <queue> -E -v ipp://<server>:631/printers/<queue> -m everywhere -D "<description>" -L "<location>"
```

Desktop alternative (GNOME): **Settings → Printers → Add Printer**, then type `ipp://<server>:631/printers/<queue>` into the search box and add the printer it finds.

### Use every queue without installing any

To make a machine use the server for all printing, with no local queues, point the CUPS client at it:

```bash
echo "ServerName <server>:631" | sudo tee /etc/cups/client.conf
```

Command-line tools and most applications then list every queue on the server. The trade-off: printers installed locally on that machine are no longer used until the line is removed.

### Check, remove and script

The commands are the same as macOS: `lpstat -v`, `lpoptions -d <queue>`, `sudo lpadmin -x <queue>`. The macOS script above works on Linux unchanged, run as root or with `sudo`.

## ChromeOS

**Not yet verified.** Printers are normally pushed from the Google Admin console, and policy may stop users adding their own.

### Quickest: one printer, for an organizational unit

Google Admin console → **Devices → Chrome → Printers**. Select the organizational unit, then **Add printer**:

- **Name:** `<queue>`, plus a description
- **Address:** `ipp://<server>:631/printers/<queue>` (in some versions: protocol IPP, address `<server>:631`, queue `printers/<queue>`)
- Choose the **driverless** option rather than a make and model

Notes:

- `ipp://` is unencrypted but needs no certificate. `ipps://` needs a certificate the Chromebooks trust, normally a publicly trusted one.
- The same page has a **Print servers** tab. Adding `ipp://<server>:631` there offers every queue on the server to the selected unit.
- On a single Chromebook, if policy allows users to add printers: **Settings → Advanced → Print and scan → Printers → Add printer → Add manually**, with the same address.

### Script: several printers

The Admin SDK Directory API creates printers in bulk (`batchCreatePrinters`). It can be called directly or through tools such as GAM. Check the current API reference before relying on the field names below.

```http
POST https://admin.googleapis.com/admin/directory/v1/customers/my_customer/chrome/printers:batchCreatePrinters
Content-Type: application/json

{
  "requests": [
    {
      "parent": "customers/my_customer",
      "printer": {
        "displayName": "<queue1>",
        "description": "<description>",
        "uri": "ipp://<server>:631/printers/<queue1>",
        "useDriverlessConfig": true,
        "orgUnitId": "<org-unit-id>"
      }
    }
  ]
}
```

Add one entry to `requests` per queue.

## User names in the usage reports

Each job is recorded under the user name the client sends, and the report groups people by that name, ignoring any `DOMAIN\` prefix and letter case.

- **Windows** sends the signed-in account, for example `AzureAD\JaneDoe`. **Verified**
- **macOS and Linux** send the local account's short name.
- **ChromeOS:** not yet known.

If the same person uses different account names on different platforms, they appear as separate people on the report.

## Troubleshooting

Check from the client that the queue is reachable:

```powershell
# Windows. curl.exe checks the certificate against the Windows certificate store.
Test-NetConnection <server> -Port 631
curl.exe -s -o NUL -w "%{http_code}`n" https://<server>:631/printers/<queue>
curl.exe -s -o NUL -w "%{http_code}`n" http://<server>:631/printers/<queue>
```

```bash
# macOS / Linux
curl -s -o /dev/null -w "%{http_code}\n" http://<server>:631/printers/<queue>
```

| Result | Meaning |
|---|---|
| `200` | Reachable and allowed. Install problems are on the client. |
| `403` | The client's subnet isn't in `CUPS_PRINT_SUBNETS`. |
| `400` | The name used isn't `CUPS_SERVER_NAME` or one of `CUPS_SERVER_ALIASES`. Use the server's name, not its IP. |
| `404` | No queue by that name. Check the Printers page. |
| `000` on `https` only, `200` on `http` | The device doesn't trust the certificate, or the certificate doesn't name `<server>`. Run `curl.exe` without `-s` to see the error, and check the certificate as shown under Windows. |
| Connection fails | DNS, routing or a firewall between the client and port 631. |

If the printer installs but nothing prints, look for the job on the server's **Jobs** tab. If it's there, the problem is between the server and the printer. If it isn't, the job never left the client.
