#!/bin/bash
set -e

ADMIN_USER="${CUPS_ADMIN_USER:-printadmin}"
ADMIN_PASSWORD="${CUPS_ADMIN_PASSWORD:-}"

if ! id "$ADMIN_USER" &>/dev/null; then
    echo "Creating CUPS admin user: $ADMIN_USER"
    useradd -m "$ADMIN_USER"
    usermod -aG lpadmin "$ADMIN_USER"

    if [ -n "$ADMIN_PASSWORD" ]; then
        echo "${ADMIN_USER}:${ADMIN_PASSWORD}" | chpasswd
    else
        RANDOM_PASS=$(openssl rand -base64 18)
        echo "${ADMIN_USER}:${RANDOM_PASS}" | chpasswd
        echo "=========================================================="
        echo "CUPS_ADMIN_PASSWORD was not set in the environment."
        echo "Generated random password for ${ADMIN_USER}: ${RANDOM_PASS}"
        echo "Save this now — it will not be shown again."
        echo "=========================================================="
    fi
else
    echo "Admin user $ADMIN_USER already exists, skipping creation."
fi

# Render cupsd.conf from the template on every start. Site values come from .env,
# so nothing environment-specific is committed, and repo changes to the template
# take effect even though /etc/cups is a persistent volume.
: "${CUPS_ALLOWED_SUBNET:?CUPS_ALLOWED_SUBNET must be set in .env}"
sed "s#@CUPS_ALLOWED_SUBNET@#${CUPS_ALLOWED_SUBNET}#g" \
    /opt/cups/cupsd.conf.template > /etc/cups/cupsd.conf
chown root:lp /etc/cups/cupsd.conf
chmod 640 /etc/cups/cupsd.conf

exec /usr/sbin/cupsd -f
