#!/bin/bash
set -e

ADMIN_USER="${CUPS_ADMIN_USER:-printadmin}"
ADMIN_PASSWORD="${CUPS_ADMIN_PASSWORD:-}"

# --- Admin account ---------------------------------------------------------
# The account lives in the container's /etc/passwd, so it is recreated
# whenever the container is. The password from .env is applied on every
# start, so rotating it only takes an edit to .env and a restart.
if ! id "$ADMIN_USER" &>/dev/null; then
    echo "Creating CUPS admin user: $ADMIN_USER"
    useradd -m -s /usr/sbin/nologin "$ADMIN_USER"
    NEW_USER=1
fi
usermod -aG lpadmin "$ADMIN_USER"

if [ -n "$ADMIN_PASSWORD" ]; then
    echo "${ADMIN_USER}:${ADMIN_PASSWORD}" | chpasswd
    echo "Admin password for ${ADMIN_USER} applied from environment."
elif [ -n "${NEW_USER:-}" ]; then
    RANDOM_PASS=$(openssl rand -base64 18)
    echo "${ADMIN_USER}:${RANDOM_PASS}" | chpasswd
    echo "=========================================================="
    echo "CUPS_ADMIN_PASSWORD was not set in the environment."
    echo "Generated random password for ${ADMIN_USER}: ${RANDOM_PASS}"
    echo "Save this now — it will not be shown again."
    echo "Set CUPS_ADMIN_PASSWORD in .env to make it persist across rebuilds."
    echo "=========================================================="
fi

# --- cupsd.conf --------------------------------------------------------------
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

# Validate a space- or comma-separated list of hostnames (letters, digits,
# dots, hyphens). Echoes the cleaned, space-separated list.
check_hostnames() {
    local name="$1" list="${2//,/ }" out="" h
    for h in $list; do
        if [[ ! "$h" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$ ]]; then
            echo "ERROR: invalid hostname '$h' in $name" >&2
            return 1
        fi
        out+="$h "
    done
    printf '%s' "${out% }"
}

# ServerName defaults to the container hostname; aliases are optional.
SERVER_NAME=$(check_hostnames CUPS_SERVER_NAME "${CUPS_SERVER_NAME:-$(hostname)}")
SERVER_ALIASES=$(check_hostnames CUPS_SERVER_ALIASES "${CUPS_SERVER_ALIASES:-}")
SERVER_ALIASES="${SERVER_NAME}${SERVER_ALIASES:+ $SERVER_ALIASES}"

awk -v print_allow="$PRINT_ALLOW" -v admin_allow="$ADMIN_ALLOW" \
    -v server_name="$SERVER_NAME" -v server_aliases="$SERVER_ALIASES" '
    /^[[:space:]]*@PRINT_ALLOW@[[:space:]]*$/ { print print_allow; next }
    /^[[:space:]]*@ADMIN_ALLOW@[[:space:]]*$/ { print admin_allow; next }
    /^ServerName @SERVER_NAME@$/              { print "ServerName " server_name; next }
    /^ServerAlias @SERVER_ALIASES@$/          { print "ServerAlias " server_aliases; next }
    { print }
' /opt/cups/cupsd.conf.template > /etc/cups/cupsd.conf
chown root:lp /etc/cups/cupsd.conf
chmod 640 /etc/cups/cupsd.conf

# Refuse to start on a config cupsd can't parse, rather than starting
# with whatever it falls back to.
if ! /usr/sbin/cupsd -t -c /etc/cups/cupsd.conf; then
    echo "ERROR: rendered /etc/cups/cupsd.conf failed validation (cupsd -t)" >&2
    exit 1
fi

# --- Usage reports -----------------------------------------------------------
# CUPS serves the usage reports as static files from <DocumentRoot>/usage, where
# docker-compose mounts them. Warn if they are mounted somewhere CUPS won't look.
CUPS_FILES_CONF=/etc/cups/cups-files.conf
DOCROOT=$(sed -n 's/^DocumentRoot[[:space:]]\{1,\}\(\/[^[:space:]]*\).*/\1/p' "$CUPS_FILES_CONF" 2>/dev/null | tail -n 1)
if [ -z "$DOCROOT" ]; then
    # Not set: the commented-out line in the stock file shows the built-in default.
    DOCROOT=$(sed -n 's/^#[[:space:]]*DocumentRoot[[:space:]]\{1,\}\(\/[^[:space:]]*\).*/\1/p' "$CUPS_FILES_CONF" 2>/dev/null | tail -n 1)
fi
if [ -n "$DOCROOT" ] && [ ! -d "$DOCROOT/usage" ]; then
    echo "WARNING: the usage reports are not mounted where CUPS serves web pages,"
    echo "so /usage/ will return Not Found. Add this line to .env and run"
    echo "'docker compose up -d':  CUPS_DOCROOT=$DOCROOT"
fi

exec /usr/sbin/cupsd -f
