#!/usr/bin/env bash
# Works out which releases the publish workflow builds, and the image tags each
# one gets. Prints a JSON array for the build matrix: one entry per release, as
# {"version", "ref", "cups_tags", "usage_tags"} (tags newline-separated). Both
# images of a release are built in one job, so a channel never ends up with a
# new image of one and an old image of the other.
#
# Inputs (environment):
#   EVENT        release | schedule | workflow_dispatch
#   RELEASE_TAG  the published release's tag (release events)
#   PRERELEASE   true | false, GitHub's pre-release box (release events)
#   IMAGE_BASE   e.g. ghcr.io/<owner>/printserver; images are <base>-cups and <base>-usage
#   BUILD_DATE   YYYYMMDD (defaults to today, UTC)
#
# Channels follow the tag names, so a server needs only the tag to follow one:
#   prod  the newest release vX.Y.Z
#   dev   the newest release of all, including pre-releases vX.Y.Z-<suffix>
# Every build also gets its version tag (moved by each monthly rebuild) and a
# dated version tag that never moves, for rollback to an exact image.
set -euo pipefail

: "${EVENT:?}" "${IMAGE_BASE:?}"
date=${BUILD_DATE:-$(date -u +%Y%m%d)}
release='^v[0-9]+\.[0-9]+\.[0-9]+$'
pre='^v[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+$'

newest() {    # newest tag matching the regex in $1; '~' sorts before the release
    git tag -l 'v*' | grep -E "$1" | sed 's/-/~/' | sort -V | tail -n 1 | sed 's/~/-/' || true
}
prod=$(newest "$release")
dev=$(newest "($release)|($pre)")

builds=()
if [ "$EVENT" = release ]; then
    tag=${RELEASE_TAG:?RELEASE_TAG is required for a release event}
    if [[ $tag =~ $release ]]; then
        [ "${PRERELEASE:-false}" = false ] \
            || { echo "::error::$tag has no suffix, so it is a prod release, but it was published as a pre-release. Untick 'Set as a pre-release', or name it like $tag-rc.1." >&2; exit 1; }
    elif [[ $tag =~ $pre ]]; then
        [ "${PRERELEASE:-false}" = true ] \
            || { echo "::error::$tag has a suffix, so it is a dev release, but it was not published as a pre-release. Tick 'Set as a pre-release', or name it vX.Y.Z." >&2; exit 1; }
    else
        echo "::error::release tag '$tag' is not vX.Y.Z or vX.Y.Z-<suffix>; nothing will be published" >&2
        exit 1
    fi
    builds+=("$tag")
fi
for t in "$prod" "$dev"; do
    [ -n "$t" ] || continue
    [[ " ${builds[*]} " == *" $t "* ]] || builds+=("$t")
done

{
    for v in "${builds[@]}"; do
        channels=()
        [ "$v" = "$prod" ] && channels+=(prod)
        [ "$v" = "$dev" ] && channels+=(dev)
        tags_for() {
            local name="$IMAGE_BASE-$1" out ch
            out="$name:$v"$'\n'"$name:$v-$date"
            for ch in "${channels[@]}"; do out+=$'\n'"$name:$ch"; done
            printf '%s' "$out"
        }
        jq -nc --arg version "$v" --arg ref "refs/tags/$v" \
            --arg cups "$(tags_for cups)" --arg usage "$(tags_for usage)" \
            '{version: $version, ref: $ref, cups_tags: $cups, usage_tags: $usage}'
    done
} | jq -sc .
