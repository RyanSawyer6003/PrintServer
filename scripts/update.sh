#!/usr/bin/env bash
# Update the print server to the newest published images for IMAGE_TAG.
#
# A run pulls the images, recreates the containers that changed, and checks
# the result the same way a person would after a deploy: both containers
# healthy, the default route on the service network, and no forwarding between
# the two networks. If a check fails, it puts the previous images back and
# says so. Volumes are never touched.
#
# It refuses to run without a backup from the last 36 hours, so a bad update
# can always be undone from a backup as well. Settings come from .env; the
# procedure is in docs/deploy.md ("Updates").
set -euo pipefail

UNIT=printserver-update             # systemd unit and state directory name
HEALTH_WAIT=${UPDATE_HEALTH_WAIT:-300}   # seconds to wait for both containers to be healthy
IMAGE_MAX_AGE_DAYS=45               # a channel image older than this means the monthly publish stopped

SELF=$(readlink -f "${BASH_SOURCE[0]}")
REPO_DIR=$(dirname "$(dirname "$SELF")")
ENV_FILE=$REPO_DIR/.env
STATE_DIR=${UPDATE_STATE_DIR:-/var/lib/$UNIT}

usage() {
    cat <<EOF
Usage: $(basename "$SELF") <command>

  run [--skip-backup-check]
                        Pull the images for IMAGE_TAG, recreate what changed, verify,
                        and roll back if the checks fail
  status [DAYS]         Exit 0 if a run succeeded in the last DAYS (default 40) and the
                        running channel image is less than $IMAGE_MAX_AGE_DAYS days old;
                        1 if not; 2 if no run is recorded. For monitoring
  install-timer [HH:MM] Install the monthly systemd timer: the first Saturday of each
                        month at HH:MM host time (default 03:30)
  remove-timer          Stop and remove the timer
EOF
}

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Print KEY's value from .env, read the way Compose reads it (as in backup.sh).
env_get() {
    local line val
    [ -f "$ENV_FILE" ] || return 0
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE" | tail -n 1) || return 0
    line=${line%$'\r'}
    val=${line#*=}
    case $val in
        \"*) val=${val#\"}; val=${val%\"*} ;;
        \'*) val=${val#\'}; val=${val%\'*} ;;
        *)   val=${val%%[[:space:]]#*}
             val=${val%"${val##*[![:space:]]}"} ;;
    esac
    printf '%s' "$val"
}

compose() { (cd "$REPO_DIR" && docker compose "$@"); }

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

# Days since the image was built, from the label the publish workflow sets.
# Prints nothing for an image without the label (a local build).
image_age_days() {
    local created ts
    created=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.created"}}' "$1" 2>/dev/null) || return 0
    [ -n "$created" ] && [ "$created" != "<no value>" ] || return 0
    ts=$(date -d "$created" +%s 2>/dev/null) || return 0
    echo $(( ($(date +%s) - ts) / 86400 ))
}

lock() {
    mkdir -p "$STATE_DIR" 2>/dev/null || die "can't create $STATE_DIR. Run with sudo."
    exec 9>"$STATE_DIR/lock" || die "can't write to $STATE_DIR. Run with sudo."
    flock -n 9 || die "another update is running"
}

# Wait until every container of the stack reports healthy. Returns 1 on timeout
# or if a container stops.
wait_healthy() {
    local deadline=$(( $(date +%s) + HEALTH_WAIT )) ids id state all
    while :; do
        ids=$(compose ps -aq)
        [ -n "$ids" ] || { log "no containers found"; return 1; }
        all=1
        for id in $ids; do
            state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
            case $state in
                running/healthy|running/none) ;;
                running/*) all= ;;
                *) log "container $(docker inspect -f '{{.Name}}' "$id") is ${state%%/*}"; return 1 ;;
            esac
        done
        [ -n "$all" ] && return 0
        [ "$(date +%s)" -lt "$deadline" ] || { log "not healthy after $HEALTH_WAIT seconds"; return 1; }
        sleep 10
    done
}

# The checks from the README's "Routing" section.
verify() {
    local cid gw route fwd
    wait_healthy || return 1
    cid=$(compose ps -q cups)
    [ -n "$cid" ] || { log "the cups container isn't running"; return 1; }
    gw=$(env_get SERVICES_GATEWAY)
    route=$(docker exec "$cid" ip route) || { log "couldn't read the container's routes"; return 1; }
    grep -q "^default via $gw " <<<"$route" \
        || { log "the default route isn't via SERVICES_GATEWAY ($gw):"; log "$route"; return 1; }
    fwd=$(docker exec "$cid" cat /proc/sys/net/ipv4/ip_forward | tr -d '\r')
    [ "$fwd" = 0 ] || { log "the container forwards between its networks (ip_forward=$fwd)"; return 1; }
    log "checks passed: containers healthy, default route via $gw, forwarding off"
}

cmd_run() {
    local skip_backup='' ref refs old new changed=() i
    while [ $# -gt 0 ]; do
        case $1 in
            --skip-backup-check) skip_backup=1 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    [ -f "$ENV_FILE" ] || die "$ENV_FILE not found"
    lock

    if [ -z "$skip_backup" ]; then
        "$REPO_DIR/scripts/backup.sh" status \
            || die "no successful backup in the last 36 hours; not updating. Run a backup first (sudo scripts/backup.sh run), or pass --skip-backup-check."
    fi

    mapfile -t refs < <(compose config --images)
    [ ${#refs[@]} -gt 0 ] || die "couldn't read the image names. Run 'docker compose config' in $REPO_DIR to see why."
    declare -A before=()
    for ref in "${refs[@]}"; do before[$ref]=$(image_id "$ref"); done

    log "pulling: ${refs[*]}"
    compose pull --quiet || die "pull failed; nothing was changed"

    for ref in "${refs[@]}"; do
        new=$(image_id "$ref")
        if [ "$new" != "${before[$ref]}" ]; then
            changed+=("$ref")
            log "new image: $ref"
        fi
    done
    [ ${#changed[@]} -gt 0 ] || log "images unchanged"

    compose up -d

    if verify; then
        for ref in "${changed[@]}"; do
            old=${before[$ref]}
            [ -n "$old" ] && docker image rm "$old" >/dev/null 2>&1 || true
        done
        date +%s > "$STATE_DIR/last-success"
        for ref in "${refs[@]}"; do
            i=$(image_age_days "$ref")
            [ -z "$i" ] || log "$ref was built $i days ago"
        done
        log "OK: update finished"
        return 0
    fi

    # A check failed: put the previous images back under the same names.
    log "checks failed; rolling back"
    local restorable=''
    for ref in "${changed[@]}"; do
        old=${before[$ref]}
        if [ -n "$old" ]; then
            docker tag "$old" "$ref" && restorable=1 && log "restored $ref to the previous image"
        else
            log "no previous image of $ref on this host to go back to"
        fi
    done
    if [ -n "$restorable" ]; then
        compose up -d
        if verify; then
            die "the update was rolled back and the previous images are running. Pin IMAGE_TAG to the version that works until the cause is found."
        fi
        die "rolled back, but the checks still fail. The cause isn't the new images; see 'docker compose logs'."
    fi
    die "the checks fail and there was nothing to roll back to. See 'docker compose logs'."
}

cmd_status() {
    local days=${1:-40} stamp now when ref age tag problems=0
    [[ $days =~ ^[0-9]+$ ]] || die "DAYS must be a whole number"
    stamp=$(cat "$STATE_DIR/last-success" 2>/dev/null) || stamp=
    if [[ ! $stamp =~ ^[0-9]+$ ]]; then
        log "CRITICAL: no successful update is recorded on this host"
        exit 2
    fi
    now=$(date +%s)
    when=$(date -d "@$stamp" '+%Y-%m-%d %H:%M')
    if [ $(( now - stamp )) -gt $(( days * 86400 )) ]; then
        log "CRITICAL: last successful update was $when, more than $days days ago"
        problems=1
    fi
    # Channel images should be rebuilt monthly. A pinned version is meant to stay put.
    tag=$(env_get IMAGE_TAG); tag=${tag:-prod}
    if [ "$tag" = prod ] || [ "$tag" = dev ]; then
        while read -r ref; do
            [ -n "$ref" ] || continue
            age=$(image_age_days "$ref")
            if [ -n "$age" ] && [ "$age" -gt "$IMAGE_MAX_AGE_DAYS" ]; then
                log "CRITICAL: $ref was built $age days ago; the monthly publish may have stopped (Actions → Publish images)"
                problems=1
            fi
        done < <(compose config --images 2>/dev/null)
    fi
    [ "$problems" -eq 0 ] || exit 1
    log "OK: last successful update was $when"
}

cmd_install_timer() {
    local at=${1:-03:30}
    [ "$(id -u)" = 0 ] || die "run with sudo"
    [[ $at =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "the time must be HH:MM, 24-hour"
    case $SELF in
        *[[:space:]%\"\'\\]*) die "systemd can't run a script from this path: $SELF (it holds a space, quote, percent sign or backslash)" ;;
    esac
    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Print server image update
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=$SELF run
EOF
    # The first Saturday of each month, after the publish on the 1st. Not
    # Persistent: a missed run waits a month rather than restarting the print
    # server in the middle of a school day.
    cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Monthly print server image update

[Timer]
OnCalendar=Sat *-*-01..07 $at:00
Persistent=false

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$UNIT.timer"
    systemctl list-timers "$UNIT.timer" --no-pager
}

cmd_remove_timer() {
    [ "$(id -u)" = 0 ] || die "run with sudo"
    systemctl disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer"
    systemctl daemon-reload
    log "Timer removed."
}

main() {
    local cmd=${1:-help}
    [ $# -eq 0 ] || shift
    case $cmd in
        help|-h|--help) usage ;;
        run)            cmd_run "$@" ;;
        status)         cmd_status "$@" ;;
        install-timer)  cmd_install_timer "$@" ;;
        remove-timer)   cmd_remove_timer ;;
        *)              usage >&2; exit 64 ;;
    esac
}

main "$@"
