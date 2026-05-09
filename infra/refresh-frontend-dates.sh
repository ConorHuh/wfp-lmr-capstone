#!/usr/bin/env bash
#
# Refresh the dates: [...] arrays in frontend/kenya_config/layers.json from
# whatever's currently in MinIO, then rebuild + recreate the frontend
# container so the new dates land in the served bundle.
#
# Why this exists:
#   PRISM bakes layers.json into the frontend image at build time. The
#   committed dates: [...] arrays reflect whatever data was in MinIO when
#   the file was last written. After a fresh ingest with different time
#   coverage (smoke test → full run, or vice versa), the date pickers in
#   the UI go stale and the prediction overlay can't fetch.
#
# Run after the pipeline finishes (lmr-serve must be up). Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.local.yml"

cd "$REPO_ROOT"

if ! curl -sfo /dev/null http://localhost:8000/health; then
    echo "ERROR: lmr-serve isn't responding at http://localhost:8000/health"
    echo "       Bring it up first: docker compose -f $COMPOSE_FILE up -d lmr-serve"
    exit 1
fi

python3 - <<'PY'
import json
import urllib.request

LAYERS_PATH = "frontend/kenya_config/layers.json"

def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())

# 1. Prediction dates (admin_level_data layers)
preds_raw = fetch("http://localhost:8000/predictions/livestock-mortality/dates")["dates"]
preds = [d.replace("_", "-") for d in preds_raw]
monthly  = sorted(d for d in preds if d.endswith("-01"))
biannual = sorted(d for d in preds if d.endswith("-02-28") or d.endswith("-09-30"))
quad     = sorted(d for d in preds if d.endswith(("-02-28", "-04-30", "-09-30", "-11-30")))

# 2. Per-dataset COG dates (static_raster layers)
collections = fetch("http://localhost:8000/collections")["collections"]
dataset_dates = {
    c["name"]: sorted(d.replace("_", "-") for d in c.get("available_dates", []))
    for c in collections
}

# layer_id (layers.json) -> ingested dataset name (datasets.yaml)
RASTER_LAYERS = {
    "modis_ndvi":      "modis-ndvi",
    "modis_evi":       "modis-evi",
    "modis_lst_day":   "modis-lst-day",
    "modis_lst_night": "modis-lst-night",
    "modis_lai":       "modis-lai",
    "modis_fpar":      "modis-fpar",
    "modis_gpp":       "modis-gpp",
    "modis_sr_red":    "modis-sr",
    "modis_sr_nir":    "modis-sr",
}

with open(LAYERS_PATH) as f:
    layers = json.load(f)

print("Prediction layers:")
for layer_id, dates in (
    ("predictions_monthly",      monthly),
    ("predictions_biannual",     biannual),
    ("predictions_quadseasonal", quad),
):
    if layer_id in layers and dates:
        layers[layer_id]["dates"] = dates
        print(f"  {layer_id:30s} {len(dates):3d} dates  {dates[0]} -> {dates[-1]}")

print("Raster layers:")
for layer_id, dataset in RASTER_LAYERS.items():
    if layer_id not in layers:
        continue
    dates = dataset_dates.get(dataset, [])
    if dates:
        layers[layer_id]["dates"] = dates
        print(f"  {layer_id:30s} {len(dates):3d} dates  {dates[0]} -> {dates[-1]}")
    else:
        print(f"  {layer_id:30s} no COGs in MinIO; left unchanged")

with open(LAYERS_PATH, "w") as f:
    json.dump(layers, f, indent=2)

print(f"\nWrote {LAYERS_PATH}")
PY

echo
echo "Rebuilding frontend image..."
docker compose -f "$COMPOSE_FILE" build frontend

echo
echo "Recreating frontend container..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate frontend

echo
echo "Done. Hard-refresh your browser (Ctrl+Shift+R) to bust the cache."
