#!/usr/bin/env bash
# Back up and restore the print server's state with restic.
#
# What is saved: the cups_config volume (queues, the server certificate and its
# key), the usage_data volume (usage history) and .env. Every other volume is
# rebuilt by the server. restic runs in a throwaway container, so the host needs
# nothing but Docker. Snapshots are encrypted with RESTIC_PASSWORD.
#
# Settings come from .env (see .env.example). The full procedure, including the
# restore, is in docs/backup.md.
set -euo pipefail

DEFAULT_IMAGE=restic/restic:0.19.1
VOLUMES=(cups_config usage_data)    # Compose volume keys
UNIT=printserver-backup             # systemd unit and state directory name

SELF=$(readlink -f "${BASH_SOURCE[0]}")
REPO_DIR=$(dirname "$(dirname "$SELF")")
ENV_FILE=$REPO_DIR/.env

usage() {
    cat <<EOF
Usage: $(basename "$SELF") <command>

  init                  Create the restic repository at RESTIC_REPOSITORY (once per target)
  run [--tag TAG]       Back up now. TAG defaults to "nightly"; only nightly snapshots
                        are thinned out by the retention rule
  status [HOURS]        Exit 0 if a backup succeeded in the last HOURS (default 36),
                        1 if it is older, 2 if none is recorded. For monitoring
  check                 Read back every byte in the repository and verify it
  snapshots             List snapshots
  restore [ID] [--force]
                        Restore .env (only if missing) and both volumes from a
                        snapshot (default: latest). The stack must be stopped.
                        --force replaces volumes that already hold data
  restic ARGS...        Run any other restic command against the repository
  install-timer [HH:MM] Install and start the nightly systemd timer (default 02:15)
  remove-timer          Stop and remove the timer
EOF
}

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Print KEY's value from .env, read the way Compose reads it: surrounding quotes
# removed, and for an unquoted value a trailing " # comment" removed.
env_get() {
    local line val
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

# Export the backup settings from .env. A variable already set in the
# environment wins, which is what lets a restore run before .env exists.
load_settings() {
    local key
    if [ -f "$ENV_FILE" ]; then
        while IFS= read -r key; do
            [ -n "${!key+set}" ] || export "$key=$(env_get "$key")"
        done < <(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?((RESTIC|AWS|B2|BACKUP)_[A-Za-z0-9_]*)=.*/\2/p' "$ENV_FILE" | sort -u)
    fi
    IMAGE=${BACKUP_IMAGE:-$DEFAULT_IMAGE}
    STATE_DIR=${BACKUP_STATE_DIR:-/var/lib/$UNIT}
}

# Check the repository settings and work out how the container reaches it.
require_repo() {
    local docker_root from=${RESTIC_FROM_REPOSITORY:-}
    [ -n "${RESTIC_REPOSITORY:-}" ] || die "RESTIC_REPOSITORY is not set. Set it in .env, or in the environment if .env doesn't exist yet."
    [ -n "${RESTIC_PASSWORD:-}" ] || die "RESTIC_PASSWORD is not set. Set it in .env, or in the environment if .env doesn't exist yet."
    REPO_ARGS=()
    case $RESTIC_REPOSITORY in
        /*)
            [ -d "$RESTIC_REPOSITORY" ] || die "RESTIC_REPOSITORY is not an existing directory. Is the backup drive mounted?"
            docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || docker_root=
            if [ "${BACKUP_ALLOW_SAME_DISK:-0}" != 1 ] && [ -d "${docker_root:-/}" ] \
                && [ "$(stat -c %d "$RESTIC_REPOSITORY")" = "$(stat -c %d "${docker_root:-/}")" ]; then
                die "RESTIC_REPOSITORY is on the same disk as the Docker volumes. Is the backup drive mounted? (BACKUP_ALLOW_SAME_DISK=1 in .env allows it.)"
            fi
            REPO_ARGS+=(-v "$RESTIC_REPOSITORY:$RESTIC_REPOSITORY")
            # restic's documentation warns about repositories on SMB shares with
            # older Linux kernels, and gives this setting as the workaround.
            case $(stat -f -c %T "$RESTIC_REPOSITORY" 2>/dev/null) in
                cifs|smb*) REPO_ARGS+=(-e GODEBUG=asyncpreemptoff=1) ;;
            esac
            # A local repository needs no network, unless a copy source does.
            case $from in ''|/*) REPO_ARGS+=(--network none) ;; esac
            ;;
        *:*) ;;    # a restic remote: s3:, b2:, sftp:, rest: ...
        *) die "RESTIC_REPOSITORY must be an absolute path, or a restic remote such as s3:... or b2:..." ;;
    esac
    # Source repository for "restic copy", when moving to a new target.
    case $from in /*) REPO_ARGS+=(-v "$from:$from:ro") ;; esac
}

# restic [docker run options ...] -- <restic arguments>
# Secrets are passed by name (-e VAR), so they never appear on a command line.
restic() {
    local -a opts=() vars=()
    local v
    while [ "$1" != -- ]; do opts+=("$1"); shift; done
    shift
    while IFS= read -r v; do vars+=(-e "$v"); done < <(compgen -e | grep -E '^(RESTIC|AWS|B2)_')
    docker run --rm "${vars[@]}" "${REPO_ARGS[@]}" "${opts[@]}" "$IMAGE" --no-cache "$@"
}

compose() { (cd "$REPO_DIR" && docker compose "$@"); }

project_name() {
    local name
    name=$(compose config 2>/dev/null | sed -n '/^name:[[:space:]]*/{s///;s/^["'\'']//;s/["'\'']$//;p;q}') || true
    [ -n "$name" ] || die "couldn't read the Compose project. Run 'docker compose config' in $REPO_DIR to see why."
    printf '%s' "$name"
}

# volume_name PROJECT KEY -> the Docker volume Compose uses for that key
volume_name() {
    local name
    name=$(docker volume ls -q --filter "label=com.docker.compose.project=$1" --filter "label=com.docker.compose.volume=$2" | head -n 1)
    printf '%s' "${name:-$1_$2}"
}

take_lock() {
    mkdir -p "$STATE_DIR" 2>/dev/null || die "can't create $STATE_DIR. Run with sudo."
    exec 9>"$STATE_DIR/lock" || die "can't write to $STATE_DIR. Run with sudo."
    flock -n 9 || die "another backup or restore is running"
}

# Stop early, with a clear message, if the repository can't be opened.
repo_ready() {
    local rc=0
    restic -- cat config >/dev/null || rc=$?
    case $rc in
        0)  ;;
        10) die "there is no restic repository at RESTIC_REPOSITORY yet. Create it once with: $SELF init" ;;
        *)  die "can't open the repository (restic exit code $rc; 12 means a wrong password)" ;;
    esac
}

cmd_run() {
    local tag=nightly project key name commit usage_running
    while [ $# -gt 0 ]; do
        case $1 in
            --tag) tag=${2:-}; shift 2 || die "--tag needs a value" ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [[ $tag =~ ^[A-Za-z0-9._-]+$ ]] || die "a tag may hold letters, digits, dots, hyphens and underscores"
    [ -f "$ENV_FILE" ] || die "$ENV_FILE not found"
    require_repo
    take_lock
    project=$(project_name)

    # "docker run -v <name>" would silently create a missing volume and back up
    # nothing, so make sure each one exists first.
    local -a mounts=(-v "$ENV_FILE:/data/env/.env:ro")
    for key in "${VOLUMES[@]}"; do
        name=$(volume_name "$project" "$key")
        docker volume inspect "$name" >/dev/null 2>&1 || die "volume $name not found. Has the stack been started on this host?"
        mounts+=(-v "$name:/data/$key:ro")
    done
    repo_ready

    local -a tags=(--tag "$tag")
    commit=$(git -C "$REPO_DIR" -c safe.directory="$REPO_DIR" rev-parse --short HEAD 2>/dev/null) || commit=
    [ -z "$commit" ] || tags+=(--tag "commit-$commit")

    # The usage service is the only writer to the usage database. Stop it for
    # the few seconds the copy takes; it isn't in the print path, so printing
    # carries on. cups_config is plain files and is copied live.
    usage_running=$(compose ps -q --status running usage 2>/dev/null) || usage_running=
    if [ -n "$usage_running" ]; then
        trap 'compose start usage' EXIT
        trap 'exit 143' TERM INT HUP
        compose stop usage
    fi
    restic "${mounts[@]}" -- backup /data --host "$project" "${tags[@]}"
    if [ -n "$usage_running" ]; then
        compose start usage
        trap - EXIT TERM INT HUP
    fi

    date +%s > "$STATE_DIR/last-success"
    log "Backup finished."

    # Thin out the nightly snapshots. Snapshots with any other tag are kept
    # until someone removes them by hand.
    if [ "$tag" = nightly ]; then
        restic -- forget --host "$project" --tag nightly --group-by host \
            --keep-daily "${BACKUP_KEEP_DAILY:-14}" \
            --keep-weekly "${BACKUP_KEEP_WEEKLY:-8}" \
            --keep-monthly "${BACKUP_KEEP_MONTHLY:-12}" \
            --prune
    fi
}

cmd_status() {
    local max=${1:-36} stamp now age when
    [[ $max =~ ^[0-9]+$ ]] || die "HOURS must be a whole number"
    stamp=$(cat "$STATE_DIR/last-success" 2>/dev/null) || stamp=
    if [[ ! $stamp =~ ^[0-9]+$ ]]; then
        log "CRITICAL: no successful backup is recorded on this host"
        exit 2
    fi
    now=$(date +%s)
    age=$(( (now - stamp) / 3600 ))
    when=$(date -d "@$stamp" '+%Y-%m-%d %H:%M')
    if [ $(( now - stamp )) -gt $(( max * 3600 )) ]; then
        log "CRITICAL: last successful backup was $when, $age hours ago (limit $max)"
        exit 1
    fi
    log "OK: last successful backup was $when, $age hours ago"
}

cmd_restore() {
    local snap=latest force=0 arg project key name tmp first i
    for arg in "$@"; do
        case $arg in
            --force) force=1 ;;
            -*) die "unknown option: $arg" ;;
            *) snap=$arg ;;
        esac
    done
    require_repo
    take_lock
    repo_ready

    if [ -e "$ENV_FILE" ]; then
        log ".env exists and was left as it is."
    else
        tmp=$(mktemp -d)
        trap "rm -rf '$tmp'" EXIT
        restic -v "$tmp:/restore" -- restore "$snap:/data/env" --target /restore
        install -m 600 "$tmp/.env" "$ENV_FILE"
        chown --reference="$REPO_DIR" "$ENV_FILE"
        rm -rf "$tmp"
        trap - EXIT
        log "Restored .env."
    fi
    project=$(project_name)

    # Check both volumes before writing to either.
    local -a names=()
    for key in "${VOLUMES[@]}"; do
        name=$(volume_name "$project" "$key")
        names+=("$name")
        if docker volume inspect "$name" >/dev/null 2>&1; then
            [ -z "$(docker ps -q --filter "volume=$name")" ] \
                || die "volume $name is in use by a running container. Stop the stack first: docker compose stop"
            if [ "$force" != 1 ]; then
                first=$(docker run --rm --network none --entrypoint /bin/sh -v "$name:/v:ro" "$IMAGE" -c 'ls -A /v | head -n 1') \
                    || die "couldn't look inside volume $name"
                [ -z "$first" ] || die "volume $name already holds data. Run again with --force to replace it with the snapshot's copy."
            fi
        else
            # Labelled the way Compose labels its own volumes, so "docker compose up" adopts it.
            docker volume create --label "com.docker.compose.project=$project" \
                --label "com.docker.compose.volume=$key" "$name" >/dev/null
        fi
    done

    # --delete removes anything the snapshot doesn't hold, so the volume ends up
    # as it was when the snapshot was taken. --verify reads the result back.
    for i in "${!VOLUMES[@]}"; do
        restic -v "${names[$i]}:/restore" -- restore "$snap:/data/${VOLUMES[$i]}" --target /restore --delete --verify
    done
    log "Restored ${VOLUMES[*]} from snapshot '$snap'."
    log "Next: docker compose up -d --build, then the checks in docs/backup.md (\"After a restore\")."
}

cmd_restic() {
    local -a tty=()
    require_repo
    if [ -t 0 ] && [ -t 1 ]; then tty=(-it); fi
    restic "${tty[@]}" -- "$@"
}

cmd_install_timer() {
    local at=${1:-02:15}
    [ "$(id -u)" = 0 ] || die "run with sudo"
    [[ $at =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "the time must be HH:MM, 24-hour"
    case $SELF in
        *[[:space:]%\"\'\\]*) die "systemd can't run a script from this path: $SELF (it holds a space, quote, percent sign or backslash)" ;;
    esac
    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Print server backup (restic)
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=$SELF run
Nice=10
IOSchedulingClass=idle
EOF
    cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Nightly print server backup

[Timer]
OnCalendar=*-*-* $at:00
RandomizedDelaySec=10min
Persistent=true

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
    log "Timer removed. Snapshots and settings are untouched."
}

main() {
    local cmd=${1:-help}
    [ $# -eq 0 ] || shift
    case $cmd in
        help|-h|--help) usage; return ;;
        install-timer)  cmd_install_timer "$@"; return ;;
        remove-timer)   cmd_remove_timer; return ;;
    esac
    load_settings
    case $cmd in
        init)      require_repo; restic -- init ;;
        run)       cmd_run "$@" ;;
        status)    cmd_status "$@" ;;
        check)     require_repo; restic -- check --read-data ;;
        snapshots) require_repo; restic -- snapshots ;;
        restore)   cmd_restore "$@" ;;
        restic)    cmd_restic "$@" ;;
        *)         usage >&2; exit 64 ;;
    esac
}

main "$@"
