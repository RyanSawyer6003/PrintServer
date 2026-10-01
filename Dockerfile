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
    && rm -rf /var/lib/apt/lists/*

COPY cupsd.conf.template /opt/cups/cupsd.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 631
ENTRYPOINT ["/entrypoint.sh"]
