#!/usr/bin/env bash
# Checks the ignore list in .grype.yaml: every entry has a reason ending in
# "review by YYYY-MM-DD", and that date hasn't passed.
#
#   scripts/ci/check-grype-ignores.sh [file]     # default: .grype.yaml
#
# An expired entry fails CI on purpose: check whether the finding still doesn't
# apply, then move the date forward or remove the entry.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
file=${1:-.grype.yaml}
[ -f "$file" ] || { echo "::error::$file not found"; exit 1; }

today=$(date -u +%F)
fail=0
entries=0
vuln=""

while IFS= read -r line; do
    case $line in
        *"- vulnerability:"*)
            [ -z "$vuln" ] || { echo "::error file=$file::$vuln has no reason"; fail=1; }
            vuln=$(sed -E 's/.*vulnerability:[[:space:]]*//; s/["'\'']//g' <<<"$line")
            entries=$((entries + 1)) ;;
        *"reason:"*)
            [ -n "$vuln" ] || continue
            date=$(grep -oE 'review by [0-9]{4}-[0-9]{2}-[0-9]{2}' <<<"$line" | grep -oE '[0-9-]{10}$')
            if [ -z "$date" ]; then
                echo "::error file=$file::$vuln: reason has no \"review by YYYY-MM-DD\""
                fail=1
            elif [[ "$date" < "$today" ]]; then
                echo "::error file=$file::$vuln: review date $date has passed; recheck it, then move the date or remove the entry"
                fail=1
            else
                echo "$vuln: ignored until $date"
            fi
            vuln="" ;;
    esac
done < <(grep -vE '^[[:space:]]*#' "$file")

[ -z "$vuln" ] || { echo "::error file=$file::$vuln has no reason"; fail=1; }
echo "$entries ignore entries checked"
exit "$fail"
