#!/usr/bin/env bash
# Works out which releases the publish workflow builds, and the image tags each
# one gets. Prints a JSON array for the build matrix: one entry per release, as
# {"version", "ref", "cups_tags", "usage_tags", "only_if_changed", "compare_tag"}
# (tags newline-separated). Both images of a release are built in one job, so a
# channel never ends up with a new image of one and an old image of the other.
#
# Inputs (environment):
#   EVENT            release | schedule | workflow_dispatch ("release" also for a
#                    dev build, which dev-release.yml passes in as a release)
#   RELEASE_TAG      the published release's tag (release events)
#   PRERELEASE       true | false, GitHub's pre-release box (release events)
#   CHANNELS         rebuilds only: which channel heads to rebuild, "dev", "prod"
#                    or "dev prod" (default "dev prod")
#   ONLY_IF_CHANGED  rebuilds only: true (default) to push only when the image
#                    differs from the channel's published one (scripts/ci/image-changed.sh)
#   PROD_BRANCH      the stable branch (default main). A vX.Y.Z release must be
#                    on it; a pre-release may come from any branch
#   IMAGE_BASE       e.g. ghcr.io/<owner>/printserver; images are <base>-cups and <base>-usage
#   BUILD_DATE       YYYYMMDD (defaults to today, UTC)
#
# Channels follow the tag names, so a server needs only the tag to follow one:
#   prod  the newest release vX.Y.Z
#   dev   the newest release of all, including pre-releases vX.Y.Z-<suffix>
# Every build also gets a dated version tag that never moves, for rollback to an
# exact image, and its version tag, which moves with rebuilds. A rebuild only
# moves the tags of the channels it was asked for: the nightly dev rebuild never
# changes what prod servers pull, even when both channels are on one version.
set -euo pipefail

: "${EVENT:?}" "${IMAGE_BASE:?}"
date=${BUILD_DATE:-$(date -u +%Y%m%d)}
prod_branch=${PROD_BRANCH:-main}
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
        # Only tested code reaches prod: the release has to be on the stable branch.
        if git rev-parse -q --verify "refs/remotes/origin/$prod_branch" >/dev/null; then
            git merge-base --is-ancestor "refs/tags/$tag^{commit}" "refs/remotes/origin/$prod_branch" \
                || { echo "::error::$tag is not on $prod_branch. A vX.Y.Z release is tagged on $prod_branch, after the change is merged there. Delete the release and its tag, merge, and publish it again." >&2; exit 1; }
        else
            echo "::warning::no $prod_branch branch to check $tag against; publishing it anyway" >&2
        fi
    elif [[ $tag =~ $pre ]]; then
        [ "${PRERELEASE:-false}" = true ] \
            || { echo "::error::$tag has a suffix, so it is a dev release, but it was not published as a pre-release. Tick 'Set as a pre-release', or name it vX.Y.Z." >&2; exit 1; }
    else
        echo "::error::release tag '$tag' is not vX.Y.Z or vX.Y.Z-<suffix>; nothing will be published" >&2
        exit 1
    fi
    builds+=("$tag")
    channels=(prod dev)     # a release moves whichever channel it now heads
    only_if_changed=false   # and is always published
else
    # Scheduled or by hand: rebuild the current head of each channel asked for.
    # A release builds only itself, so publishing one never changes another
    # channel's images.
    read -r -a channels <<<"${CHANNELS:-dev prod}"
    for ch in "${channels[@]}"; do
        case $ch in
            prod) t=$prod ;;
            dev)  t=$dev ;;
            *)    echo "::error::unknown channel '$ch' in CHANNELS; expected dev and/or prod" >&2; exit 1 ;;
        esac
        [ -n "$t" ] || continue
        [[ " ${builds[*]-} " == *" $t "* ]] || builds+=("$t")
    done
    only_if_changed=${ONLY_IF_CHANGED:-true}
    [[ $only_if_changed =~ ^(true|false)$ ]] || { echo "::error::ONLY_IF_CHANGED must be true or false" >&2; exit 1; }
fi

asked() { [[ " ${channels[*]} " == *" $1 "* ]]; }

{
    for v in "${builds[@]+"${builds[@]}"}"; do
        heads=()
        [ "$v" = "$prod" ] && asked prod && heads+=(prod)
        [ "$v" = "$dev" ] && asked dev && heads+=(dev)
        # The version tag of a prod release moves only with prod, so a server
        # pinned to v1.2.0 isn't updated by the nightly dev rebuild.
        move_version=true
        [[ $v =~ $release ]] && ! asked prod && move_version=false
        tags_for() {
            local name="$IMAGE_BASE-$1" out ch
            out="$name:$v-$date"
            [ "$move_version" = true ] && out+=$'\n'"$name:$v"
            for ch in "${heads[@]+"${heads[@]}"}"; do out+=$'\n'"$name:$ch"; done
            printf '%s' "$out"
        }
        # A rebuild is compared with the image its first channel points to now.
        compare=${heads[0]:-$v}
        jq -nc --arg version "$v" --arg ref "refs/tags/$v" \
            --arg cups "$(tags_for cups)" --arg usage "$(tags_for usage)" \
            --argjson only "$only_if_changed" --arg compare "$compare" \
            '{version: $version, ref: $ref, cups_tags: $cups, usage_tags: $usage,
              only_if_changed: $only, compare_tag: $compare}'
    done
} | jq -sc .
