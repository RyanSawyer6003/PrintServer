#!/usr/bin/env bash
# Fails if site-specific values have crept into the repository.
#
#   scripts/ci/check-site-values.sh                 # tracked files only
#   scripts/ci/check-site-values.sh <rev-range>     # also commit messages and identities
#
# Checks:
#   - Private IPv4 addresses. Only the placeholder ranges used throughout the
#     docs and tests are allowed: 10.10.x.x, 10.20.x.x, 10.30.x.x, 10.40.x.x.
#   - Host and domain names (tracked files only). Allowed: example.org/.com/.net
#     and their subdomains, and the public names in scripts/ci/allowed-domains.txt.
#   - SITE_DENYLIST (optional, set as a repository secret in CI): real values
#     that must never appear, such as the district's domain, server names and
#     subnets. Space-, comma- or newline-separated; matched case-insensitively
#     as plain text, in files and in commits. It is never stored in the repo.
#
# Commit messages and author/committer identities get the address and denylist
# checks but not the domain check, so an ordinary work email doesn't fail it.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
range=${1:-}
allow_file=scripts/ci/allowed-domains.txt
fail=0

report() {    # report <where> <what>
    echo "::error::$1: $2"
    fail=1
}

# --- helpers -----------------------------------------------------------------

# Read stdin as "<where>\t<text>" lines; report private addresses outside the
# placeholder ranges.
check_addresses() {
    while IFS=$'\t' read -r where text; do
        grep -oE '(^|[^0-9.])[0-9]{1,3}(\.[0-9]{1,3}){3}([^0-9.]|$)' <<<"$text" \
            | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' \
            | while IFS=. read -r a b c d; do
                for o in "$a" "$b" "$c" "$d"; do [ "$o" -le 255 ] || continue 2; done
                case "$a.$b" in
                    10.10|10.20|10.30|10.40) continue ;;
                esac
                if [ "$a" = 10 ] || { [ "$a" = 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; } \
                    || { [ "$a" = 192 ] && [ "$b" = 168 ]; }; then
                    echo "$where	private address $a.$b.$c.$d (use 10.10/10.20/10.30/10.40 placeholders)"
                fi
            done
    done
}

# Report domains that aren't placeholders or on the allow list.
check_domains() {
    local allowed
    allowed=$(grep -vE '^[[:space:]]*(#|$)' "$allow_file" 2>/dev/null | tr '[:upper:]' '[:lower:]')
    while IFS=$'\t' read -r where text; do
        grep -oiP '(?<![A-Za-z0-9._-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?:org|com|net|edu|gov|us|io|co|local|lan|internal|home|corp|intranet)(?![A-Za-z0-9-])' <<<"$text" \
            | tr '[:upper:]' '[:lower:]' | sort -u \
            | while read -r d; do
                case "$d" in
                    example.org|example.com|example.net|*.example.org|*.example.com|*.example.net) continue ;;
                esac
                ok=
                while read -r a; do
                    [ -n "$a" ] || continue
                    if [ "$d" = "$a" ] || [[ "$d" == *".$a" ]]; then ok=1; break; fi
                done <<<"$allowed"
                [ -n "$ok" ] || echo "$where	domain '$d' (use example.org, or add a public name to $allow_file)"
            done
    done
}

# Report SITE_DENYLIST entries.
check_denylist() {
    [ -n "${SITE_DENYLIST:-}" ] || return 0
    local terms
    terms=$(tr ', ' '\n' <<<"$SITE_DENYLIST" | grep -v '^$')
    while IFS=$'\t' read -r where text; do
        while read -r t; do
            if grep -qiF -- "$t" <<<"$text"; then
                echo "$where	contains a SITE_DENYLIST value"
            fi
        done <<<"$terms"
    done
}

# Tracked text files as "<file>:<line>\t<text>".
tracked_lines() {
    git ls-files -z | while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        grep -Iq . "$f" 2>/dev/null || continue          # skip binary and empty files
        awk -v f="$f" '{ printf "%s:%d\t%s\n", f, NR, $0 }' "$f"
    done
}

# Commit messages and identities as "commit <sha>\t<text>".
commit_lines() {
    git log --format='%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x01' "$range" \
        | awk 'BEGIN { RS = "\001"; FS = "\000" }
               NF >= 4 {
                   sha = substr($1, 1, 12); gsub(/^\n/, "", sha)
                   print "commit " sha "\t" $2
                   print "commit " sha "\t" $3
                   n = split($4, l, "\n")
                   for (i = 1; i <= n; i++) if (l[i] != "") print "commit " sha "\t" l[i]
               }'
}

emit() {    # turn "<where>\t<message>" lines into errors
    while IFS=$'\t' read -r where msg; do report "$where" "$msg"; done
}

# --- run -----------------------------------------------------------------------

if [ -z "${SITE_DENYLIST:-}" ]; then
    echo "::notice::SITE_DENYLIST is not set; checking addresses and domains only."
fi

lines=$(tracked_lines)
emit < <(check_addresses <<<"$lines")
emit < <(check_domains <<<"$lines")
emit < <(check_denylist <<<"$lines")

if [ -n "$range" ]; then
    clines=$(commit_lines)
    emit < <(check_addresses <<<"$clines")
    emit < <(check_denylist <<<"$clines")
fi

if [ "$fail" -ne 0 ]; then
    echo "Site values found. The repo rule: no real hostnames, domains, addresses or NIC names in git, including commit messages and identities."
    exit 1
fi
echo "No site values found."
