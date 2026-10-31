FROM debian:trixie-slim

# apt-get upgrade: the slim base image lags Debian's security updates by up
# to a few weeks, and the vulnerability scan in CI counts its packages too.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    cups \
    cups-filters \
    cups-client \
    printer-driver-all \
    avahi-daemon \
    snmp \
    iputils-ping \
    iproute2 \
    curl \
    openssl \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Remove Notifiers to mitigate CVE-2026-105326 and CVE-2026-34978
# Remove FOOMATIC-RIP to mitigate CVE-2026-55453 and CVE-2026-55467
RUN rm -f /usr/lib/cups/notifier/mailto /usr/lib/cups/notifier/rss \
          /usr/lib/cups/filter/foomatic-rip

# IPP only: remove every CUPS backend except ipp, ipps (a link to ipp) and snmp.
# cupsd refuses a device URI whose backend isn't installed, so AppSocket
# (socket://), LPD, http://, USB, mDNS and the rest can't be added, from the web
# interface or lpadmin, and the web interface doesn't offer them. snmp stays for
# printer discovery; snmp.conf makes it offer only IPP addresses. The build
# stops if anything else is left, so a package update can't quietly add one back.
RUN cd /usr/lib/cups/backend \
    && for f in * .[!.]*; do \
         [ -e "$f" ] || [ -L "$f" ] || continue; \
         case "$f" in ipp|ipps|snmp) ;; *) rm -rf -- "$f" ;; esac; \
       done \
    && [ -x ipp ] && [ -x snmp ] && [ "$(readlink ipps)" = ipp ] \
    && [ "$(ls -A | sort | tr '\n' ' ')" = "ipp ipps snmp " ]
COPY snmp.conf.template /opt/cups/snmp.conf.template

# Add "Status" and "Usage" to the navigation bar of the CUPS web pages. The
# build stops here if a CUPS update changes the bar, so the links can't
# silently disappear.
COPY nav-links.sh /opt/printserver/nav-links.sh
RUN bash /opt/printserver/nav-links.sh

# The print server theme (usage/theme/): added to the end of CUPS's stylesheet,
# so the CUPS pages look like the staff page and usage reports. CUPS's own
# rules stay in place underneath; nothing else about the pages changes.
COPY usage/theme/tokens.css usage/theme/cups.css /opt/printserver/theme/
RUN css=/usr/share/cups/doc-root/cups.css \
    && [ -f "$css" ] \
    && { printf '\n/* --- Print server theme (usage/theme) --- */\n'; \
         cat /opt/printserver/theme/tokens.css /opt/printserver/theme/cups.css; } >> "$css" \
    && grep -q -- '--- Print server theme' "$css"

# Printer up/down check, started by the entrypoint. Standard library only.
COPY status/statuscheck.py /opt/printserver/statuscheck.py
RUN chmod 755 /opt/printserver/statuscheck.py \
    && ln -s /opt/printserver/statuscheck.py /usr/local/bin/printer-status \
    && mkdir -p /var/lib/printserver/status
ENV PYTHONDONTWRITEBYTECODE=1

COPY cupsd.conf.template /opt/cups/cupsd.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 631
ENTRYPOINT ["/entrypoint.sh"]
