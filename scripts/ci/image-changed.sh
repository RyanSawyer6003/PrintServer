#!/usr/bin/env bash
# Decides whether a rebuilt image is worth publishing. Used by the publish
# workflow's scheduled rebuilds, which build every night (dev) or week (prod)
# but push only when something changed.
#
#   scripts/ci/image-changed.sh <built image> <published image>
#
# Exit 0 (publish) when the published image:
#   - doesn't exist or can't be pulled,
#   - was built from a different commit (the revision label),
#   - has a different set of Debian packages or versions, or
#   - is MAX_UNCHANGED_DAYS (default 28) days old or more, so a channel image
#     never gets older than that and `update.sh status` can still tell a stopped
#     workflow from a quiet month.
# Exit 1 (skip) when none of those hold. Exit 2 on a usage error.
#
# The package list is what matters: almost every security fix reaches the image
# as a new package version, from the base image or from apt-get at build time.
# The reason goes to stdout, with the changed packages, for the run's summary.
set -euo pipefail

built=${1:-}
published=${2:-}
[ -n "$built" ] && [ -n "$published" ] || { echo "usage: image-changed.sh <built image> <published image>" >&2; exit 2; }
max_age=${MAX_UNCHANGED_DAYS:-28}
[[ $max_age =~ ^[0-9]+$ ]] || { echo "MAX_UNCHANGED_DAYS must be a whole number" >&2; exit 2; }

label() { docker image inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true; }

packages() {
    docker run --rm --network none --entrypoint dpkg-query "$1" \
        -W -f '${Package} ${Version} ${Architecture}\n' | LC_ALL=C sort
}

if ! docker pull --quiet "$published" >/dev/null 2>&1; then
    echo "changed: $published isn't published yet"
    exit 0
fi

rev_built=$(label "$built" org.opencontainers.image.revision)
rev_pub=$(label "$published" org.opencontainers.image.revision)
if [ "$rev_built" != "$rev_pub" ]; then
    echo "changed: built from ${rev_built:-an unknown commit}, published from ${rev_pub:-an unknown commit}"
    exit 0
fi

created=$(label "$published" org.opencontainers.image.created)
if ts=$(date -d "$created" +%s 2>/dev/null); then
    age=$(( ($(date +%s) - ts) / 86400 ))
    if [ "$age" -ge "$max_age" ]; then
        echo "changed: the published image is $age days old (republished at $max_age days even without changes)"
        exit 0
    fi
else
    echo "changed: the published image has no build date"
    exit 0
fi

new=$(packages "$built")
old=$(packages "$published")
if [ "$new" != "$old" ]; then
    echo "changed: Debian packages differ"
    # Lines only in the published image (-) and only in the new build (+).
    # diff exits 1 when they differ, which is expected here. awk reads to the
    # end rather than stopping at 50 lines, so nothing upstream gets SIGPIPE.
    { diff <(echo "$old") <(echo "$new") || true; } \
        | sed -n 's/^< /- /p; s/^> /+ /p' \
        | awk 'NR <= 50; END { if (NR > 50) print "(" NR - 50 " more)" }'
    exit 0
fi

echo "unchanged: same commit and the same $(wc -l <<<"$new") packages as $published ($age days old)"
exit 1
