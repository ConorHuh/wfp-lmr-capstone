#!/usr/bin/env bash
set -euo pipefail

# Truncate coordinate precision in boundary GeoJSON files to 6 decimal places.
# Reduces file size significantly with no visible loss of accuracy (~10cm precision).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PRECISION=6

truncate_file() {
    local input="$1"
    if [[ ! -f "$input" ]]; then
        echo "File not found: $input"
        return 1
    fi

    local size_before
    size_before=$(wc -c < "$input" | tr -d ' ')

    python3 -c "
import json, sys

def truncate_coords(obj, precision=${PRECISION}):
    if isinstance(obj, float):
        return round(obj, precision)
    elif isinstance(obj, list):
        return [truncate_coords(item, precision) for item in obj]
    elif isinstance(obj, dict):
        return {k: truncate_coords(v, precision) for k, v in obj.items()}
    return obj

with open('$input') as f:
    data = json.load(f)

data = truncate_coords(data)

with open('$input', 'w') as f:
    json.dump(data, f, separators=(',', ':'))
"

    local size_after
    size_after=$(wc -c < "$input" | tr -d ' ')
    local reduction=$(( (size_before - size_after) * 100 / size_before ))
    echo "  ${input##*/}: ${size_before} -> ${size_after} bytes (${reduction}% reduction)"
}

echo "Truncating coordinate precision to ${PRECISION} decimal places..."

# Boundary file in prism-app public data
PRISM_BOUNDARY="${REPO_ROOT}/prism-app/frontend/public/data/kenya/ken_bnd_adm3_WFP.json"
if [[ -f "$PRISM_BOUNDARY" ]]; then
    truncate_file "$PRISM_BOUNDARY"
fi

# Source boundary file in kenya_config
SOURCE_BOUNDARY="${REPO_ROOT}/prism/kenya_config/admin_boundaries.geojson"
if [[ -f "$SOURCE_BOUNDARY" ]]; then
    truncate_file "$SOURCE_BOUNDARY"
fi

echo "Done."
