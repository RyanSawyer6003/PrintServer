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

# --- View-only account for the usage reports ---------------------------------
# Optional. Members of the usageviewers group can sign in to /usage/ and
# nothing else: cupsd.conf keeps /admin and every admin operation for the
# admin group. The group always exists, because cupsd.conf names it.
VIEW_GROUP=usageviewers
VIEWER_USER="${USAGE_VIEWER_USER:-}"
VIEWER_PASSWORD="${USAGE_VIEWER_PASSWORD:-}"

getent group "$VIEW_GROUP" >/dev/null || groupadd "$VIEW_GROUP"
VIEWER_OK=""
if [ -n "$VIEWER_USER" ] || [ -n "$VIEWER_PASSWORD" ]; then
    if [ -z "$VIEWER_USER" ] || [ -z "$VIEWER_PASSWORD" ]; then
        echo "WARNING: set both USAGE_VIEWER_USER and USAGE_VIEWER_PASSWORD in .env for a"
        echo "view-only login. Only one is set, so no view-only account was created."
    elif [[ ! "$VIEWER_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        echo "ERROR: USAGE_VIEWER_USER must be a lower-case account name (letters, digits, _ or -)" >&2
        exit 1
    elif [ "$VIEWER_USER" = "$ADMIN_USER" ]; then
        echo "ERROR: USAGE_VIEWER_USER must not be the admin account ($ADMIN_USER)" >&2
        exit 1
    elif id "$VIEWER_USER" &>/dev/null && { [ "$(id -u "$VIEWER_USER")" -lt 1000 ] || [ "$VIEWER_USER" = nobody ]; }; then
        echo "ERROR: USAGE_VIEWER_USER names a system account ($VIEWER_USER); choose another name" >&2
        exit 1
    else
        id "$VIEWER_USER" &>/dev/null || useradd -M -s /usr/sbin/nologin "$VIEWER_USER"
        echo "${VIEWER_USER}:${VIEWER_PASSWORD}" | chpasswd
        VIEWER_OK=1
        echo "View-only usage login enabled for ${VIEWER_USER}."
    fi
fi
# Make the group hold exactly the configured account, so a renamed or removed
# viewer loses access on the next start, and keep that account out of the
# admin group whatever was done by hand.
gpasswd -M "${VIEWER_OK:+$VIEWER_USER}" "$VIEW_GROUP" >/dev/null
if [ -n "$VIEWER_OK" ] && id -nG "$VIEWER_USER" | tr ' ' '\n' | grep -qx lpadmin; then
    gpasswd -d "$VIEWER_USER" lpadmin >/dev/null
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

# --- Usage reports and staff page ----------------------------------------------
# CUPS serves the usage reports and the staff page as static files from
# <DocumentRoot>/usage and <DocumentRoot>/status, where docker-compose mounts
# them. Warn if they are mounted somewhere CUPS won't look.
CUPS_FILES_CONF=/etc/cups/cups-files.conf
DOCROOT=$(sed -n 's/^DocumentRoot[[:space:]]\{1,\}\(\/[^[:space:]]*\).*/\1/p' "$CUPS_FILES_CONF" 2>/dev/null | tail -n 1)
if [ -z "$DOCROOT" ]; then
    # Not set: the commented-out line in the stock file shows the built-in default.
    DOCROOT=$(sed -n 's/^#[[:space:]]*DocumentRoot[[:space:]]\{1,\}\(\/[^[:space:]]*\).*/\1/p' "$CUPS_FILES_CONF" 2>/dev/null | tail -n 1)
fi
if [ -n "$DOCROOT" ] && { [ ! -d "$DOCROOT/usage" ] || [ ! -d "$DOCROOT/status" ]; }; then
    echo "WARNING: the usage reports and staff page are not mounted where CUPS serves"
    echo "web pages, so /usage/ and /status/ will return Not Found. Add this line to"
    echo ".env and run 'docker compose up -d':  CUPS_DOCROOT=$DOCROOT"
fi

# --- Printer status check ------------------------------------------------------
# Every STATUS_CHECK_MINUTES, test whether each queue's printer accepts a
# connection on its print port, and write the result for the usage service to
# put on the staff page. It runs here because this container is the one on the
# printer network. It runs as an unprivileged user, and is restarted if it exits.
for name in STATUS_CHECK_MINUTES STATUS_CHECK_TIMEOUT STATUS_DOWN_AFTER; do
    if [[ ! "${!name:-1}" =~ ^[1-9][0-9]{0,4}$ ]]; then
        echo "ERROR: $name must be a whole number of 1 or more, got '${!name}'" >&2
        exit 1
    fi
done
STATUS_DIR=/var/lib/printserver/status
mkdir -p "$STATUS_DIR"
chown nobody:nogroup "$STATUS_DIR"
chmod 755 "$STATUS_DIR"
(
    sleep 5    # let cupsd, started below, come up before the first check
    # A clean environment: the check has no use for the passwords in this one.
    while true; do
        env -i PATH="$PATH" PYTHONUNBUFFERED=1 \
            STATUS_CHECK_MINUTES="${STATUS_CHECK_MINUTES:-}" \
            STATUS_CHECK_TIMEOUT="${STATUS_CHECK_TIMEOUT:-}" \
            STATUS_DOWN_AFTER="${STATUS_DOWN_AFTER:-}" \
            setpriv --reuid=nobody --regid=nogroup --clear-groups \
            python3 /opt/printserver/statuscheck.py run || true
        sleep 15
    done
) &

exec /usr/sbin/cupsd -f
