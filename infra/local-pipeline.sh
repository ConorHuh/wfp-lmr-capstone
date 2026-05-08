#!/usr/bin/env bash
#
# Manual end-to-end pipeline runner for the local-offline stack.
#
# Subcommands:
#   ingest          Run the ingest container against external sources (Planetary
#                   Computer / NASA Earthdata / Copernicus CDS / CHIRPS HTTP)
#                   and write COGs + chirps/soil_moisture parquets to MinIO.
#                   Needs internet. Needs EARTHDATA_USERNAME, EARTHDATA_PASSWORD,
#                   CDSAPI_KEY env vars set.
#   parquets        Build the missing parquets (NDVI, LST, SR_NIR, SR_SWIR1,
#                   JRC, WorldCover) from the COGs the ingest step wrote.
#                   No internet needed.
#   features        Build ward features for all 3 schemes from the parquets.
#   infer           Run inference for biannual + quadseasonal + monthly.
#   all             ingest -> parquets -> features -> infer.
#
# Window: TIME_START / TIME_END env vars (YYYY-MM). Default = last 24 months
# ending last full month.
#
# Usage:
#   ./infra/local-pipeline.sh ingest
#   ./infra/local-pipeline.sh all
#   TIME_START=2024-01 TIME_END=2025-12 ./infra/local-pipeline.sh features

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.local.yml}"
DC=(docker compose -f "$COMPOSE_FILE")

default_window() {
    # End = last full month; start = end - 23 months
    if [[ -z "${TIME_END:-}" ]]; then
        TIME_END="$(date -u -d 'last month' +%Y-%m)"
    fi
    if [[ -z "${TIME_START:-}" ]]; then
        # GNU date arithmetic: 23 months before TIME_END
        TIME_START="$(date -u -d "${TIME_END}-01 - 23 months" +%Y-%m)"
    fi
    export TIME_START TIME_END
}

log() { echo "[$(date +%H:%M:%S)] $*"; }

require_creds_for_ingest() {
    local missing=()
    [[ -z "${EARTHDATA_USERNAME:-}" ]] && missing+=("EARTHDATA_USERNAME")
    [[ -z "${EARTHDATA_PASSWORD:-}" ]] && missing+=("EARTHDATA_PASSWORD")
    [[ -z "${CDSAPI_KEY:-}" ]] && missing+=("CDSAPI_KEY")
    if (( ${#missing[@]} )); then
        echo "ERROR: ingest requires these env vars: ${missing[*]}" >&2
        echo "  Set them in your shell or in a .env file before running." >&2
        echo "  Earthdata: https://urs.earthdata.nasa.gov" >&2
        echo "  CDS:       https://cds.climate.copernicus.eu/profile" >&2
        exit 1
    fi
}

cmd_ingest() {
    require_creds_for_ingest
    log "Ingest: pulling COGs and writing chirps/soil_moisture parquets"
    "${DC[@]}" run --rm \
        -e EARTHDATA_USERNAME -e EARTHDATA_PASSWORD \
        -e CDSAPI_URL -e CDSAPI_KEY \
        lmr-cli --mode ingest
}

cmd_parquets() {
    log "Building remaining parquets (NDVI, LST, SR_NIR, SR_SWIR1, JRC, WorldCover)"
    # Use the uv-managed venv python; container deps live in /app/.venv, not system.
    "${DC[@]}" run --rm \
        --entrypoint /app/.venv/bin/python \
        lmr-cli /app/infra/build_parquets.py
}

cmd_features() {
    default_window
    log "Feature extraction: $TIME_START -> $TIME_END (all 3 schemes in one pass)"
    "${DC[@]}" run --rm lmr-cli \
        --mode feature-extract \
        --time-start "$TIME_START" --time-end "$TIME_END"
}

cmd_infer() {
    log "Running inference for all 3 schemes"
    for scheme in biannual quadseasonal monthly; do
        log "  -> $scheme"
        "${DC[@]}" run --rm lmr-cli --mode infer --scheme "$scheme"
    done
}

cmd_all() {
    cmd_ingest
    cmd_parquets
    cmd_features
    cmd_infer
    log "Pipeline complete. Predictions are in s3://lmr-data-cogs-local/predictions/"
}

main() {
    local sub="${1:-}"
    case "$sub" in
        ingest)   cmd_ingest ;;
        parquets) cmd_parquets ;;
        features) cmd_features ;;
        infer)    cmd_infer ;;
        all)      cmd_all ;;
        ""|-h|--help)
            sed -n '2,/^set -/p' "$0" | sed 's/^# \{0,1\}//'
            ;;
        *)
            echo "Unknown subcommand: $sub" >&2
            exit 1
            ;;
    esac
}

main "$@"
