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

# Build "Allow from" lines from a space- or comma-separated list of IPv4 CIDRs.
# Every entry is validated so a typo can't silently open or break access.
render_allow() {
    local name="$1" list="${2//,/ }" out="" s
    if [ -z "${list// /}" ]; then
        echo "ERROR: $name must be set in .env (space-separated CIDRs)" >&2
        return 1
    fi
    for s in $list; do
        if [[ ! "$s" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; then
            echo "ERROR: invalid CIDR '$s' in $name" >&2
            return 1
        fi
        out+="  Allow from $s"$'\n'
    done
    printf '%s' "$out"
}

PRINT_ALLOW=$(render_allow CUPS_PRINT_SUBNETS "${CUPS_PRINT_SUBNETS:-}")
ADMIN_ALLOW=$(render_allow CUPS_ADMIN_SUBNETS "${CUPS_ADMIN_SUBNETS:-}")

awk -v print_allow="$PRINT_ALLOW" -v admin_allow="$ADMIN_ALLOW" '
    /^[[:space:]]*@PRINT_ALLOW@[[:space:]]*$/ { print print_allow; next }
    /^[[:space:]]*@ADMIN_ALLOW@[[:space:]]*$/ { print admin_allow; next }
    { print }
' /opt/cups/cupsd.conf.template > /etc/cups/cupsd.conf
chown root:lp /etc/cups/cupsd.conf
chmod 640 /etc/cups/cupsd.conf

exec /usr/sbin/cupsd -f
