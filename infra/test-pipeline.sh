#!/usr/bin/env bash
#
# End-to-end validation of the local-offline stack.
#
# Usage:
#   ./infra/test-pipeline.sh --window 2months   # smoke test, ~30-60 min
#   ./infra/test-pipeline.sh --window 2years    # full run, several hours
#
# Optional flags:
#   --skip-bootstrap    don't re-upload bundle (skip if MinIO already has it)
#   --skip-ingest       skip ingest entirely (use existing COGs in MinIO)
#
# Prerequisites checked at startup:
#   - Docker daemon running (Docker Desktop on macOS, or Colima, or WSL2 + Docker)
#   - Repo is the current dir's grandparent
#   - .env file at repo root with EARTHDATA_USERNAME / PASSWORD / CDSAPI_KEY
#   - Bundle at $BUNDLE_DIR (default ~/lmr-bundle/) with the canonical layout
#
# Exit code 0 = full pass; non-zero = a stage failed (see colored ✗ marks).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.local.yml"
BUNDLE_DIR="${BUNDLE_DIR:-$HOME/lmr-bundle}"
WINDOW=""
SKIP_BOOTSTRAP=0
SKIP_INGEST=0

usage() {
    sed -n '2,/^set -/p' "$0" | sed 's|^# \{0,1\}||' | head -n 20
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --window)         WINDOW="$2"; shift 2 ;;
        --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
        --skip-ingest)    SKIP_INGEST=1; shift ;;
        -h|--help)        usage ;;
        *) echo "Unknown arg: $1"; usage ;;
    esac
done

[[ "$WINDOW" =~ ^(2months|2years)$ ]] || { echo "ERROR: --window must be 2months or 2years"; usage; }

log()  { printf "\n\033[1;36m=== %s ===\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; }
die()  { err "$@"; exit 1; }

# ─── pre-flight ──────────────────────────────────────────────────────────────
log "Pre-flight checks"
command -v docker >/dev/null              || die "docker not found in PATH"
docker compose version >/dev/null 2>&1    || die "docker compose plugin not available"
docker info >/dev/null 2>&1               || die "Docker daemon not reachable (start Docker Desktop / Colima)"
[[ -f "$COMPOSE_FILE" ]]                  || die "compose file missing: $COMPOSE_FILE"
[[ -f "${REPO_ROOT}/.env" ]]              || die ".env missing at repo root: ${REPO_ROOT}/.env"
[[ -d "$BUNDLE_DIR/inference_bundle/biannual" ]] \
    && [[ -d "$BUNDLE_DIR/inference_bundle/quadseasonal" ]] \
    && [[ -d "$BUNDLE_DIR/inference_bundle/monthly" ]] \
    && [[ -f "$BUNDLE_DIR/geoBoundaries-KEN-ADM3.geojson" ]] \
    || die "bundle layout wrong at $BUNDLE_DIR (expected inference_bundle/{biannual,quadseasonal,monthly}/ + geoBoundaries-KEN-ADM3.geojson). Override with BUNDLE_DIR=..."
ok "Tools, repo, .env, and bundle all present"

cd "$REPO_ROOT"
START_TS=$(date +%s)
PIPELINE="${REPO_ROOT}/infra/local-pipeline.sh"
DC=(docker compose -f "$COMPOSE_FILE")

# ─── MinIO ───────────────────────────────────────────────────────────────────
log "Starting MinIO"
"${DC[@]}" up -d minio
for i in {1..15}; do
    if curl -sfo /dev/null http://localhost:9000/minio/health/live; then break; fi
    sleep 2
done
curl -sfo /dev/null http://localhost:9000/minio/health/live \
    || die "MinIO did not come up healthy in 30s"
ok "MinIO healthy at :9000 (console :9001, login minioadmin/minioadmin)"

# ─── bootstrap ───────────────────────────────────────────────────────────────
if [[ $SKIP_BOOTSTRAP -eq 0 ]]; then
    log "Bootstrap — uploading bundle to MinIO"
    BUNDLE_DIR="$BUNDLE_DIR" "${REPO_ROOT}/infra/local-bootstrap.sh"
    ok "Bundle uploaded"
else
    ok "Skipping bootstrap (--skip-bootstrap)"
fi

# ─── ingest ──────────────────────────────────────────────────────────────────
if [[ $SKIP_INGEST -eq 0 ]]; then
    if [[ "$WINDOW" == "2months" ]]; then
        # 65 days back so we comfortably catch 2 monthly cycles
        if date -u -v-65d '+%Y-%m-%d' >/dev/null 2>&1; then
            START_DATE=$(date -u -v-65d '+%Y-%m-%d')   # BSD date (macOS)
        else
            START_DATE=$(date -u -d '65 days ago' '+%Y-%m-%d')   # GNU date (Linux/WSL)
        fi
        log "Ingest — 2-month smoke (start_date=$START_DATE → today)"
        "${DC[@]}" --profile cli run --rm \
            -e EARTHDATA_USERNAME -e EARTHDATA_PASSWORD \
            -e CDSAPI_URL -e CDSAPI_KEY \
            lmr-cli --mode ingest --start-date "$START_DATE"

        # Static datasets (jrc-water 2020, worldcover 2021) fall outside the
        # 65-day smoke window. Run a second pass with NO --start-date so they
        # pull via their full lookback_days=36500. Already-ingested dynamic
        # datasets get skipped via the existing-dates check inside cli.py —
        # only the statics actually do work here (~3 min).
        log "Ingest pass 2 — pull static datasets (jrc-water, worldcover)"
        "${DC[@]}" --profile cli run --rm \
            -e EARTHDATA_USERNAME -e EARTHDATA_PASSWORD \
            -e CDSAPI_URL -e CDSAPI_KEY \
            lmr-cli --mode ingest
    else
        log "Ingest — full 24 months (uses lookback_days=730 from datasets.yaml)"
        "${DC[@]}" --profile cli run --rm \
            -e EARTHDATA_USERNAME -e EARTHDATA_PASSWORD \
            -e CDSAPI_URL -e CDSAPI_KEY \
            lmr-cli --mode ingest
    fi
    ok "Ingest complete"
else
    ok "Skipping ingest (--skip-ingest)"
fi

# ─── parquets ────────────────────────────────────────────────────────────────
log "Building wide-format parquets from COGs"
"$PIPELINE" parquets
ok "Parquets built"

# ─── clear feature-extract cache (per-collection extracted parquets) ─────────
# Without this, re-runs hit cached results from the previous window and
# downstream features come out wrong (we hit this bug during dev).
log "Clearing feature-extract cache"
docker run --rm --network wfp-lmr-capstone_lmr-net \
    -e MC_HOST_lmr=http://minioadmin:minioadmin@minio:9000 \
    --entrypoint mc minio/mc:latest \
    rm --recursive --force lmr/lmr-data-cogs-local/inference/ 2>&1 | tail -5 || true
ok "Cache cleared"

# ─── feature extraction (auto-window from chirps parquet contents) ───────────
log "Feature extraction"
WINDOW_LINE=$("${DC[@]}" --profile cli run --rm \
    --entrypoint /app/.venv/bin/python lmr-cli -c "
import boto3, io, pandas as pd
s3 = boto3.client('s3')
obj = s3.get_object(Bucket='lmr-data-cogs-local', Key='parquets/chirps.parquet')
df = pd.read_parquet(io.BytesIO(obj['Body'].read()))
months = sorted(c for c in df.columns if c not in {'lat','lon','variable','collection'})
print(f'{months[0]} {months[-1]}')
" 2>/dev/null | tail -1)
TIME_START=$(awk '{print $1}' <<<"$WINDOW_LINE")
TIME_END=$(awk '{print $2}' <<<"$WINDOW_LINE")
[[ -n "$TIME_START" && -n "$TIME_END" ]] \
    || die "Could not read date range from chirps.parquet (ingest probably wrote no data)"
echo "  Window: $TIME_START → $TIME_END"
TIME_START="$TIME_START" TIME_END="$TIME_END" "$PIPELINE" features
ok "Features extracted"

# ─── inference (3 schemes) ───────────────────────────────────────────────────
log "Inference — biannual / quadseasonal / monthly"
"$PIPELINE" infer
ok "Inference complete (predictions in s3://lmr-data-cogs-local/predictions/)"

# ─── serve + frontend ───────────────────────────────────────────────────────
log "Starting lmr-serve + frontend"
"${DC[@]}" up -d lmr-serve frontend
sleep 5

# ─── endpoint validation ─────────────────────────────────────────────────────
log "Endpoint checks"
fails=0
checks=0
check() {
    local label="$1" url="$2"
    checks=$((checks+1))
    local code; code=$(curl -s -o /dev/null -w "%{http_code}" "$url" || echo 000)
    if [[ "$code" == "200" ]]; then ok "$label  HTTP 200"; else err "$label  HTTP $code"; fails=$((fails+1)); fi
}
check "serve /health"                              http://localhost:8000/health
check "serve /collections"                         http://localhost:8000/collections
check "serve /predictions/livestock-mortality/dates" http://localhost:8000/predictions/livestock-mortality/dates
check "frontend /"                                 http://localhost:3000/
check "frontend /data/kenya/dashboard.json"        http://localhost:3000/data/kenya/dashboard.json

# Pull a sample prediction date (whatever the API surfaces) and verify it returns ward data
SAMPLE_DATE=$(curl -sf http://localhost:8000/predictions/livestock-mortality/dates 2>/dev/null \
    | python3 -c "import sys,json; ds=json.load(sys.stdin)['dates']; print(ds[-1] if ds else '')" 2>/dev/null || true)
if [[ -n "$SAMPLE_DATE" ]]; then
    URL="http://localhost:8000/predictions/livestock-mortality-monthly/${SAMPLE_DATE}"
    check "serve /predictions/.../monthly/$SAMPLE_DATE"  "$URL"
fi

# ─── summary ─────────────────────────────────────────────────────────────────
log "Summary"
N_PRED=$(curl -sf http://localhost:8000/predictions/livestock-mortality/dates 2>/dev/null \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin)['dates']))" 2>/dev/null || echo 0)
ELAPSED=$(( $(date +%s) - START_TS ))
ELAPSED_HMS=$(printf '%02d:%02d:%02d' $((ELAPSED/3600)) $((ELAPSED%3600/60)) $((ELAPSED%60)))
echo "  Window:                  $WINDOW"
echo "  Feature time range:      $TIME_START → $TIME_END"
echo "  Prediction dates output: $N_PRED"
echo "  Endpoint checks:         $((checks-fails))/$checks passed"
echo "  Elapsed:                 $ELAPSED_HMS"
echo

if [[ $fails -eq 0 && $N_PRED -gt 0 ]]; then
    ok "ALL GREEN — open http://localhost:3000 in your browser"
    exit 0
else
    err "VALIDATION FAILED — see errors above"
    exit 1
fi
