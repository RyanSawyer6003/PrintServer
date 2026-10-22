#!/usr/bin/env bash
# Smoke test for a built image. Used by both CI workflows.
#
#   scripts/ci/smoke-test.sh cups  <image>
#   scripts/ci/smoke-test.sh usage <image>
#
# cups:  the unit tests for the status check, on the image's own Python; then
#        the real entrypoint (cupsd.conf rendered and checked with cupsd -t),
#        the scheduler answering, the Status and Usage links in the CUPS pages
#        (the navigation patch against Debian's packaged templates), /admin
#        refusing plain http and asking for a login over https, and the
#        container not forwarding.
# usage: the unit tests on the image's own Python, then one ingest-and-render
#        pass under the restrictions docker-compose.yml runs it with.
#
# The unit tests come from SMOKE_SRC (default: this checkout), so the publish
# workflow can test a release's own tests with this script from main.
# Everything here is a placeholder: no site values.
set -euo pipefail

kind=${1:?usage: smoke-test.sh cups|usage <image>}
image=${2:?usage: smoke-test.sh cups|usage <image>}
src=$(cd "${SMOKE_SRC:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}" && pwd)

pass() { echo "ok   $*"; }
die()  { echo "::error::smoke test ($kind): $*"; exit 1; }

case $kind in
cups)
    echo "--- status check unit tests"
    docker run --rm --network none -v "$src/status:/t:ro" -w /t \
        -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python3 "$image" \
        -m unittest discover -s /t -v

    net=printserver-ci-$$
    name=printserver-ci-cups-$$
    ip=10.30.99.10
    name_on_cert=direct.example.org
    cleanup() {
        if [ "${status:-0}" -ne 0 ]; then
            echo "--- container log"; docker logs "$name" 2>&1 | tail -n 60 || true
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
        docker network rm "$net" >/dev/null 2>&1 || true
    }
    trap 'status=$?; cleanup' EXIT

    docker network create --subnet 10.30.99.0/24 "$net" >/dev/null
    docker run -d --name "$name" --network "$net" --ip "$ip" \
        --hostname "$name_on_cert" \
        --sysctl net.ipv4.ip_forward=0 \
        -e CUPS_SERVER_NAME="$name_on_cert" \
        -e CUPS_PRINT_SUBNETS=10.30.99.0/24 \
        -e CUPS_ADMIN_SUBNETS=10.30.99.0/24 \
        -e CUPS_ADMIN_PASSWORD="ci-$(openssl rand -hex 12)" \
        "$image" >/dev/null

    echo "--- waiting for the scheduler"
    up=
    for _ in $(seq 1 30); do
        [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ] || die "container exited"
        if docker exec "$name" lpstat -r 2>/dev/null | grep -qx 'scheduler is running'; then up=1; break; fi
        sleep 2
    done
    [ -n "$up" ] || die "scheduler not running after 60 seconds"
    pass "entrypoint rendered cupsd.conf, cupsd -t passed, scheduler running"

    c=(curl -sS --max-time 10 --resolve "$name_on_cert:631:$ip")
    page=$("${c[@]}" -w '\n%{http_code}' "http://$name_on_cert:631/printers/")
    code=${page##*$'\n'}
    [ "$code" = 200 ] || die "/printers/ from a print subnet returned $code, expected 200"
    grep -q 'href="/status/"' <<<"$page" || die "Status link missing from the CUPS navigation bar"
    grep -q 'href="/usage/"' <<<"$page"  || die "Usage link missing from the CUPS navigation bar"
    pass "/printers/ loads; navigation bar has Status and Usage"

    code=$("${c[@]}" -o /dev/null -w '%{http_code}' "http://$name_on_cert:631/admin/")
    [ "$code" != 200 ] || die "/admin/ answered over plain http"
    pass "/admin/ over http refused ($code)"

    code=$("${c[@]}" -k -o /dev/null -w '%{http_code}' "https://$name_on_cert:631/admin/")
    [ "$code" = 401 ] || die "/admin/ over https without a login returned $code, expected 401"
    pass "/admin/ over https asks for a login"

    fwd=$(docker exec "$name" cat /proc/sys/net/ipv4/ip_forward)
    [ "$fwd" = 0 ] || die "ip_forward is $fwd"
    pass "container does not forward"
    ;;

usage)
    echo "--- usage unit tests"
    docker run --rm --network none -v "$src/usage:/t:ro" -w /t \
        -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python3 "$image" \
        -m unittest discover -s /t -v

    echo "--- one pass, restricted as in docker-compose.yml"
    docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true \
        -e USAGE_MONTHLY_PAGES=500 -e TZ=America/Chicago \
        --tmpfs /var/log/cups --tmpfs /data --tmpfs /reports --tmpfs /staff --tmpfs /status-data \
        "$image" once
    pass "usage once ran with a read-only root, no network and no capabilities"
    ;;

*)
    die "unknown image kind '$kind'"
    ;;
esac
