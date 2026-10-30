#!/usr/bin/env bash
# Works out the tag for this week's dev build of the dev branch, used by
# .github/workflows/dev-release.yml. Prints the tag, or nothing when there is
# nothing to release (the reason goes to stderr).
#
# Dev builds are named vX.Y.Z-dev.YYYYMMDD and sort after every existing
# release, so each one becomes the dev channel (scripts/ci/release-plan.sh):
#   - after a dev build, the next one keeps its version: v1.0.2-dev.20261102
#     follows v1.0.2-dev.20261026
#   - after anything else (a release, or a hand-made pre-release such as
#     v1.0.2-rc.1), the next one moves to the following patch: v1.0.3-dev.<date>
# A release or rc made by hand for v1.0.2 sorts after v1.0.2-dev.* builds,
# because "rc" sorts after "dev" and a release sorts after its pre-releases.
#
# The newest release is found among all tags, not by walking this branch's
# history, so a vX.Y.Z hotfix made on main is seen too: the next dev build moves
# past it, and dev never sorts below prod. After dev is merged into main and
# released, nothing on dev is new, so the next week is skipped.
#
# Inputs (environment): BUILD_DATE (YYYYMMDD, default today, UTC).
# Run from a full clone with tags (fetch-depth: 0), on the commit to release.
set -euo pipefail

date=${BUILD_DATE:-$(date -u +%Y%m%d)}
any='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

newest=$(git tag -l 'v*' | grep -E "$any" | sed 's/-/~/' | sort -V | tail -n 1 | sed 's/~/-/' || true)

if [ -z "$newest" ]; then
    echo "no vX.Y.Z release yet; publish the first release by hand" >&2
    exit 0
fi

if [ "$(git rev-list --count "$newest..HEAD")" -eq 0 ]; then
    echo "nothing merged since $newest" >&2
    exit 0
fi

core=${newest#v}; core=${core%%-*}
if [[ ! $newest =~ -dev\.[0-9]{8}$ ]]; then
    IFS=. read -r major minor patch <<<"$core"
    core="$major.$minor.$((patch + 1))"
fi
tag="v$core-dev.$date"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    echo "$tag already exists" >&2
    exit 0
fi
echo "$tag"
