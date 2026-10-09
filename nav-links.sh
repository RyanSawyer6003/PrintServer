#!/bin/bash
# Adds "Status" and "Usage" links to the navigation bar of the CUPS web pages.
# Run once at image build time, from the Dockerfile.
#
# CUPS builds its pages from template files. The bar is in header.tmpl (every
# page the server generates) and in the static home page, index.html. Both get
# the two links after the "Printers" entry. CUPS also ships translated copies
# of both files; those are patched the same way.
#
# The script fails, and so does the image build, if the English header or home
# page can't be patched: a CUPS update that changes the bar is then caught at
# build time. A translated copy that doesn't match is reported and left alone.
set -euo pipefail

TEMPLATES="${CUPS_TEMPLATES:-/usr/share/cups/templates}"
DOCROOT="${CUPS_DOCROOT:-/usr/share/cups/doc-root}"

# The "Printers" entry of the bar, in any language.
ANCHOR='<li><a [^>]*href="/printers/">[^<]*</a></li>'

STATUS_LINK='<li><a href="/status/">Status</a></li>'
# /usage/ is HTTPS only. Sent there from an http page, CUPS redirects to its IP
# address, which doesn't match the certificate. The onclick goes straight to
# https on the name the browser is already using. It has no braces, because
# header.tmpl is a CUPS template and braces mean something there.
USAGE_LINK='<li><a href="/usage/" onclick="if(location.protocol=='"'http:'"')return !(location.href='"'https://'"'+location.host+'"'/usage/'"')">Usage</a></li>'

patch_file() {
    local file="$1" count
    if grep -q 'href="/status/"' "$file" && grep -q 'href="/usage/"' "$file"; then
        return 0    # already patched
    fi
    count=$(grep -cE "$ANCHOR" "$file" || true)
    [ "$count" = 1 ] || return 1
    awk -v anchor="$ANCHOR" -v status="$STATUS_LINK" -v usage="$USAGE_LINK" '
        { print }
        $0 ~ anchor {
            indent = $0; sub(/<li>.*/, "", indent)
            print indent status
            print indent usage
        }
    ' "$file" > "$file.new"
    cat "$file.new" > "$file"    # keep the original owner and mode
    rm -f "$file.new"
    grep -qF "$STATUS_LINK" "$file" && grep -qF "$USAGE_LINK" "$file"
}

for required in "$TEMPLATES/header.tmpl" "$DOCROOT/index.html"; do
    if [ ! -f "$required" ]; then
        echo "ERROR: $required not found; the CUPS web files have moved." >&2
        exit 1
    fi
    if ! patch_file "$required"; then
        echo "ERROR: could not add the Status and Usage links to $required." >&2
        echo "The CUPS navigation bar has changed; update nav-links.sh to match." >&2
        exit 1
    fi
    echo "navigation links added: $required"
done

for translated in "$TEMPLATES"/*/header.tmpl "$DOCROOT"/*/index.html; do
    [ -f "$translated" ] || continue
    if patch_file "$translated"; then
        echo "navigation links added: $translated"
    else
        echo "WARNING: navigation links not added to $translated (bar not recognised)"
    fi
done
