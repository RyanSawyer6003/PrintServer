FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Add "Status" and "Usage" to the navigation bar of the CUPS web pages. The
# build stops here if a CUPS update changes the bar, so the links can't
# silently disappear.
COPY nav-links.sh /opt/printserver/nav-links.sh
RUN bash /opt/printserver/nav-links.sh

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
