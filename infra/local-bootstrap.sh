#!/usr/bin/env bash
#
# Local-offline bootstrap for the LMR platform.
#
# Reads model bundle + ward-boundary GeoJSON from $BUNDLE_DIR (default:
# ~/lmr-bundle/) and uploads them to the local MinIO bucket the stack uses.
#
# Idempotent — safe to re-run after replacing the bundle with a newer one.
#
# Prereqs (set up by docker compose -f docker-compose.local.yml up -d minio):
#   - MinIO reachable at http://localhost:9000 with minioadmin / minioadmin
#
# Usage:
#   ./infra/local-bootstrap.sh                   # uses ~/lmr-bundle/
#   BUNDLE_DIR=/path/to/bundle ./infra/local-bootstrap.sh

set -euo pipefail

BUNDLE_DIR="${BUNDLE_DIR:-$HOME/lmr-bundle}"
# Endpoint as seen from the mc container (talks to minio over the compose network)
MINIO_ENDPOINT_INTERNAL="${MINIO_ENDPOINT_INTERNAL:-http://minio:9000}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASS="${MINIO_PASS:-minioadmin}"
BUCKET="${BUCKET:-lmr-data-cogs-local}"
COMPOSE_NETWORK="${COMPOSE_NETWORK:-wfp-lmr-capstone_lmr-net}"

REQUIRED_BUNDLE_FILES=(
    "inference_bundle/biannual/feature_names.json"
    "inference_bundle/biannual/xgboost_model.joblib"
    "inference_bundle/biannual/lgbm_model.joblib"
    "inference_bundle/biannual/rf_model.joblib"
    "inference_bundle/biannual/ridge_model.joblib"
    "inference_bundle/biannual/feature_scaler.joblib"
    "inference_bundle/biannual/train_medians.json"
    "inference_bundle/biannual/run_metadata.json"
    "inference_bundle/biannual/ensemble_weights.json"

    "inference_bundle/quadseasonal/feature_names.json"
    "inference_bundle/quadseasonal/xgboost_model.joblib"
    "inference_bundle/quadseasonal/lgbm_model.joblib"
    "inference_bundle/quadseasonal/rf_model.joblib"
    "inference_bundle/quadseasonal/ridge_model.joblib"
    "inference_bundle/quadseasonal/feature_scaler.joblib"
    "inference_bundle/quadseasonal/train_medians.json"
    "inference_bundle/quadseasonal/run_metadata.json"
    "inference_bundle/quadseasonal/ensemble_weights.json"

    "inference_bundle/monthly/feature_names.json"
    "inference_bundle/monthly/xgboost_model.joblib"
    "inference_bundle/monthly/lgbm_model.joblib"
    "inference_bundle/monthly/rf_model.joblib"
    "inference_bundle/monthly/ridge_model.joblib"
    "inference_bundle/monthly/feature_scaler.joblib"
    "inference_bundle/monthly/train_medians.json"
    "inference_bundle/monthly/run_metadata.json"
    "inference_bundle/monthly/ensemble_weights.json"
    "inference_bundle/monthly/meta_model.joblib"
    "inference_bundle/monthly/meta_scaler.joblib"
    "inference_bundle/monthly/meta_feature_names.json"
    "inference_bundle/monthly/ward_encoding.json"

    "geoBoundaries-KEN-ADM3.geojson"
)

log() { echo "[$(date +%H:%M:%S)] $*"; }
err() { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; }

# Run mc via Docker on the compose network. Bundle dir is bind-mounted at /bundle.
# Avoids needing a local mc install (which would require sudo on most systems).
# MC_HOST_lmr is the magic env var mc reads to register an alias 'lmr' on the
# fly — saves us the chicken-and-egg of needing persistent ~/.mc/config.json.
mc_docker() {
    docker run --rm \
        --network "$COMPOSE_NETWORK" \
        -v "$BUNDLE_DIR:/bundle:ro" \
        -e "MC_HOST_lmr=http://$MINIO_USER:$MINIO_PASS@minio:9000" \
        --entrypoint mc \
        minio/mc:latest "$@"
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        err "docker not found — install Docker Desktop and enable WSL integration"
        exit 1
    fi
    if ! docker network inspect "$COMPOSE_NETWORK" >/dev/null 2>&1; then
        err "compose network '$COMPOSE_NETWORK' not found"
        err "Run 'docker compose -f docker-compose.local.yml up -d minio' first"
        exit 1
    fi
}

verify_bundle_layout() {
    log "Verifying $BUNDLE_DIR/ layout"
    if [[ ! -d "$BUNDLE_DIR" ]]; then
        err "$BUNDLE_DIR does not exist."
        err "Drop the model bundle there. Expected layout:"
        err "  $BUNDLE_DIR/"
        err "  ├── inference_bundle/{biannual,quadseasonal,monthly}/<artifacts>"
        err "  └── geoBoundaries-KEN-ADM3.geojson"
        exit 1
    fi
    local missing=()
    for f in "${REQUIRED_BUNDLE_FILES[@]}"; do
        if [[ ! -f "$BUNDLE_DIR/$f" ]]; then
            missing+=("$f")
        fi
    done
    if (( ${#missing[@]} )); then
        err "Bundle is missing ${#missing[@]} required file(s):"
        for f in "${missing[@]}"; do err "  $f"; done
        exit 1
    fi
    log "Bundle OK"
}

ensure_bucket() {
    log "Configuring mc alias 'lmr' -> $MINIO_ENDPOINT_INTERNAL"
    if mc_docker ls "lmr/$BUCKET" >/dev/null 2>&1; then
        log "Bucket '$BUCKET' already exists"
    else
        log "Creating bucket '$BUCKET'"
        mc_docker mb "lmr/$BUCKET"
    fi
}

upload_bundle() {
    log "Uploading inference bundle to s3://$BUCKET/models/inference_bundle/"
    mc_docker cp --recursive \
        "/bundle/inference_bundle/" \
        "lmr/$BUCKET/models/inference_bundle/"

    log "Uploading ward boundaries to s3://$BUCKET/models/"
    mc_docker cp \
        "/bundle/geoBoundaries-KEN-ADM3.geojson" \
        "lmr/$BUCKET/models/geoBoundaries-KEN-ADM3.geojson"
}

main() {
    require_docker
    verify_bundle_layout
    ensure_bucket
    upload_bundle
    log "Bootstrap complete. Bucket s3://$BUCKET is ready."
    log "Next: ./infra/local-pipeline.sh ingest"
}

main "$@"
