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

A queue can be addressed in three ways. All three point at the same queue:

| Form | Used by |
|---|---|
| `http://<server>:631/printers/<queue>` | Windows (the recommended method below) |
| `ipp://<server>:631/printers/<queue>` | macOS, Linux, ChromeOS. Unencrypted. |
| `ipps://<server>:631/printers/<queue>` | Encrypted. Clients must trust the server's certificate, and by default CUPS uses a self-signed one. |

## Find printers on the server

### Web interface

Open `http://<server>:631/printers/` in a browser on a client subnet. No login is needed to view it. **Verified**

- The **Printers** tab lists every queue with its description, location, model and status. Click a queue to open its page; that page's address is the queue's install URL.
- The **Jobs** tab shows queued and completed jobs.
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

### Quickest: one printer

PowerShell, as the signed-in user. **Verified:** works for a standard user with no elevation.

```powershell
Add-Printer -Name "<queue>" -PortName "http://<server>:631/printers/<queue>" -DriverName "Microsoft IPP Class Driver"
```

The printer appears at once with the name given in `-Name`.

**Same result without PowerShell. Verified.** Go to **Settings → Bluetooth & devices → Printers & scanners → Add device**. Choose **Add manually** (or "The printer that I want isn't listed"), then **Select a shared printer by name**. Enter `http://<server>:631/printers/<queue>`. When asked for a driver, choose **Microsoft**, then **Microsoft IPP Class Driver**. Check the name Windows gives the printer and rename it if needed.

### Things to know

- **The printer is installed for the whole device**, even when a standard user adds it. Every user of that computer sees it. Jobs are still recorded under the name of the person printing.
- **Use `http://` unless the server has a certificate Windows trusts.** With the default self-signed certificate, `https://` fails on this method, which means print data crosses the network unencrypted. Using `https://` with a trusted certificate is **not yet verified**.
- **The driver must already be on the device.** Check with `Get-PrinterDriver -Name "Microsoft IPP Class Driver"`. A standard user can't add a driver. If it's missing, add it once from an elevated session with `Add-PrinterDriver -Name "Microsoft IPP Class Driver"`.
- **Avoid `Add-Printer -IppURL`** and the "Add a printer using an IP address or hostname" option for this server. They need administrator rights, take minutes to finish, name every printer "Generic IPP Everywhere Printer", and use a `WSD-<GUID>` port that can't be traced back to a queue.
- **LPR/LPD, raw port 9100, and Standard TCP/IP ports don't work.** The server only accepts IPP on port 631.

### Check and remove

```powershell
# Which printers go through the server
Get-Printer | Where-Object PortName -like "*://<server>:631/printers/*" |
    Format-Table Name, PortName, DriverName -AutoSize

# Remove one
Remove-Printer -Name "<queue>"
```

Printers on a bare IP address or a `WSD-...` port do not go through the server, and their jobs won't appear in the usage reports.

### Script: several printers

Install script for any deployment tool (Intune, an RMM, a logon script). It can be run any number of times: it adds missing queues and never removes or renames anything. **Not yet verified as a script.** The command inside it is verified as the signed-in user. Running it as SYSTEM is untested.

```powershell
# Install-PrintQueues.ps1
$Server = "<server>"
$Scheme = "http"                         # "https" only with a certificate the clients trust
$Queues = @("<queue1>", "<queue2>")
$Driver = "Microsoft IPP Class Driver"

if (-not (Get-PrinterDriver -Name $Driver -ErrorAction SilentlyContinue)) {
    try   { Add-PrinterDriver -Name $Driver -ErrorAction Stop }      # needs elevation
    catch { Write-Output "Driver '$Driver' is missing and could not be added: $($_.Exception.Message)"; exit 1 }
}

$problems = @()
foreach ($q in $Queues) {
    $port     = "${Scheme}://${Server}:631/printers/$q"
    $existing = Get-Printer -Name $q -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.PortName -ne $port) { $problems += "$q : name already used by a printer on port $($existing.PortName)" }
        continue
    }
    try   { Add-Printer -Name $q -PortName $port -DriverName $Driver -ErrorAction Stop; Write-Output "Added $q" }
    catch { $problems += "$q : $($_.Exception.Message)" }
}

if ($problems) { Write-Output ($problems -join "`n"); exit 1 }
exit 0
```

For a deployment tool that runs a detection script first (such as Intune Remediations), use this with the same three variables:

```powershell
# Detect-PrintQueues.ps1 - exit 0 when every queue is installed, 1 otherwise
$Server = "<server>"
$Scheme = "http"
$Queues = @("<queue1>", "<queue2>")

$missing = $Queues | Where-Object {
    $p = Get-Printer -Name $_ -ErrorAction SilentlyContinue
    -not $p -or $p.PortName -ne "${Scheme}://${Server}:631/printers/$_"
}
if ($missing) { Write-Output "Missing: $($missing -join ', ')"; exit 1 }
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
# Windows
Test-NetConnection <server> -Port 631
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
| Connection fails | DNS, routing or a firewall between the client and port 631. |

If the printer installs but nothing prints, look for the job on the server's **Jobs** tab. If it's there, the problem is between the server and the printer. If it isn't, the job never left the client.
