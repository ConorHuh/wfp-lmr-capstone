#!/usr/bin/env bash
set -euo pipefail

# One-time migration: rename S3 date folders from hyphenated (2026-01-17) to
# underscored (2026_01_17) format across all prefixes in lmr-data-cogs-dev.
#
# Usage:
#   ./scripts/migrate-s3-dates.sh              # dry run (default)
#   ./scripts/migrate-s3-dates.sh --execute    # actually rename

BUCKET="lmr-data-cogs-dev"
DRY_RUN=true

if [[ "${1:-}" == "--execute" ]]; then
    DRY_RUN=false
    echo "EXECUTING — objects will be copied and originals deleted."
else
    echo "DRY RUN — pass --execute to actually rename. Showing what would change:"
fi
echo ""

# List all objects, find those with hyphenated date folders (YYYY-MM-DD)
RENAMED=0
SKIPPED=0

aws s3 ls "s3://${BUCKET}/" --recursive | awk '{print $4}' | while read -r key; do
    # Match keys containing a /YYYY-MM-DD/ segment
    if [[ "$key" =~ ^(.*/)[0-9]{4}-[0-9]{2}-[0-9]{2}(/.*)?$ ]]; then
        # Build new key by replacing hyphens in the date segment only
        new_key=$(echo "$key" | sed -E 's|/([0-9]{4})-([0-9]{2})-([0-9]{2})/|/\1_\2_\3/|')

        if [[ "$key" == "$new_key" ]]; then
            continue
        fi

        if [[ "$DRY_RUN" == true ]]; then
            echo "  $key → $new_key"
        else
            aws s3 cp "s3://${BUCKET}/${key}" "s3://${BUCKET}/${new_key}" --quiet
            aws s3 rm "s3://${BUCKET}/${key}" --quiet
            echo "  renamed: $key → $new_key"
        fi
        RENAMED=$((RENAMED + 1))
    fi
done

echo ""
echo "Total objects to rename: ${RENAMED}"
if [[ "$DRY_RUN" == true ]]; then
    echo "Run with --execute to apply."
fi
