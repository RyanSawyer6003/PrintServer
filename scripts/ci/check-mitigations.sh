#!/usr/bin/env bash
# Checks that a cups image carries the mitigations this repo relies on.
#
#   scripts/ci/check-mitigations.sh <image>
#
# The ignore list in .grype.yaml skips CVEs whose vulnerable files the
# Dockerfile deletes. Grype still matches the package, so a scan of an image
# built without the deletion (an older release, or a Dockerfile change) would
# skip real findings. This check fails in that case, so the scan can't report
# such an image as clean.
#
# Keep REMOVED in step with the "rm -f" in the Dockerfile and the ignore list.
set -uo pipefail

image=${1:?usage: check-mitigations.sh <image>}

# Files the Dockerfile deletes; .grype.yaml ignores the CVEs in them.
REMOVED=(
    /usr/lib/cups/notifier/mailto       # CVE-2026-105326, CVE-2026-34978
    /usr/lib/cups/notifier/rss          # CVE-2026-105326, CVE-2026-34978
    /usr/lib/cups/filter/foomatic-rip   # CVE-2026-55453, CVE-2026-55467
)
# Template rule that refuses temporary queues (CVE-2026-34990, -39316, -107888, -39314).
TEMPLATE=/opt/cups/cupsd.conf.template

out=$(docker run --rm --network none --entrypoint /bin/sh "$image" -c '
    t=$1; shift
    for f; do [ ! -e "$f" ] || echo "present: $f"; done
    if [ ! -f "$t" ]; then
        echo "missing: $t"
    elif ! awk "/<Limit CUPS-Create-Local-Printer>/{l=1} l&&/Deny from all/{ok=1} /<\/Limit>/{l=0} END{exit !ok}" "$t"; then
        echo "not denied: CUPS-Create-Local-Printer in $t"
    fi
' sh "$TEMPLATE" "${REMOVED[@]}") || { echo "::error::could not run $image"; exit 1; }

if [ -n "$out" ]; then
    while IFS= read -r line; do
        echo "::error::$image is missing a mitigation: $line"
    done <<<"$out"
    exit 1
fi
echo "$image: all ${#REMOVED[@]} removed files absent; temporary queues denied"
