# LMR Platform — Local-Offline Stack (`abhas-local-testing` branch)

End-to-end LMR platform running on a single laptop against a MinIO bucket.
**Storage and serving are fully local; ingest still reaches out to the public
internet** (Microsoft Planetary Computer, NASA Earthdata, Copernicus CDS,
UCSB CHIRPS) for raw remote-sensing data — same as a real customer deploy.

This branch is **deliberately divergent from `main`**. It points buckets at
`lmr-data-cogs-local`, prunes datasets to the model's actual feature
dependencies, downsamples a couple of static layers to fit in 8 GB RAM, and
fixes a handful of latent bugs that surface only when running inference
end-to-end. **Don't merge this branch into `main`** — see "How this differs
from main" below.

---

## TL;DR — getting this running

Prereqs: Docker Desktop with WSL integration on, ~80 GB free, [free NASA
Earthdata account][nasa], [free Copernicus CDS account][cds] with the
[ERA5-Land licence accepted][era5-licence], and a model bundle delivered to
you out-of-band (the `.joblib` files; not in this repo).

```bash
# 0. Branch + secrets
git checkout abhas-local-testing
cat > .env <<'EOF'
EARTHDATA_USERNAME=your_user
EARTHDATA_PASSWORD=your_pass
CDSAPI_URL=https://cds.climate.copernicus.eu/api
CDSAPI_KEY=your_personal_access_token
EOF

# 1. Drop the model bundle at ~/lmr-bundle/ (or anywhere — pass BUNDLE_DIR=)
#    Layout:
#      ~/lmr-bundle/inference_bundle/{biannual,quadseasonal,monthly}/<artifacts>
#      ~/lmr-bundle/geoBoundaries-KEN-ADM3.geojson

# 2. Bring up MinIO and upload the bundle
docker compose -f docker-compose.local.yml up -d minio
./infra/local-bootstrap.sh                 # uses ~/lmr-bundle by default
# or:  BUNDLE_DIR=/mnt/c/Users/you/lmr-bundle ./infra/local-bootstrap.sh

# 3. Run the data pipeline (ingest pulls from public internet, ~30–60 min
#    for 30 days of MODIS; full 24 months is several hours)
./infra/local-pipeline.sh ingest          # raw -> COGs in MinIO
./infra/local-pipeline.sh parquets        # COGs -> wide-format parquets
./infra/local-pipeline.sh features        # parquets -> ward-feature parquets
./infra/local-pipeline.sh infer           # ward features -> predictions
# Or do all four in one go:
./infra/local-pipeline.sh all

# 4. Bring up serve + frontend
docker compose -f docker-compose.local.yml up -d lmr-serve frontend

# 5. Open http://localhost:3000 (PRISM) and pick Marsabit County
```

When everything is running, you should see Marsabit ward boundaries on the
map and clicking through prediction layers should populate them with
green/yellow/red risk colours. If anything misbehaves, jump to
"Troubleshooting" below.

[nasa]: https://urs.earthdata.nasa.gov/users/new
[cds]: https://cds.climate.copernicus.eu/
[era5-licence]: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-monthly-means?tab=download#manage-licences

---

## What runs where

```
docker compose -f docker-compose.local.yml ...
├── minio          (S3-compatible storage, console on :9001)   :9000  /data
├── lmr-serve      (FastAPI + TiTiler tile server)             :8000
├── lmr-cli        (one-shot runs of ingest/features/infer/parquets)
└── frontend       (PRISM static build via nginx)              :3000
```

Volumes:
- `~/lmr-local-data` ↔ MinIO `/data` (data persists across restarts)
- `./infra` mounted read-only into `lmr-cli` so you can iterate on
  `build_parquets.py` without rebuilding the image

The bundle (16 MB) lives wherever you put it — bind-mounted into the
bootstrap container at run time. `/mnt/c/...` is fine for the bundle since
it's only uploaded once. Keep the larger MinIO data directory
(`~/lmr-local-data`) on WSL ext4 for IO performance.

---

## How this differs from `main`, and why

The branch makes 8 categories of change. Every one had a forcing reason
discovered during the hands-on run-through.

### 1. Bucket and CORS — point at `lmr-data-cogs-local`

`backend/config/datasets.yaml` is rewritten to reference
`lmr-data-cogs-local` instead of `lmr-data-cogs-dev`. CORS is restricted to
`localhost:3000` / `localhost:8000` instead of `*`. Reason: the entire
runtime is on one laptop; production CFN-managed buckets aren't in the
picture.

### 2. Lookback windows — 24 months everywhere instead of 1100/90 days

| Setting | Production (`main`) | Local (this branch) |
|---|---|---|
| MODIS `lookback_days` | 1100 | 730 |
| CHIRPS / ERA5 `lookback_days` | 90 | 730 |
| `inference.feature_window_months` | 36 | 24 |

24 months is the minimum where anomaly baselines (`ndvi_anom`,
`soil_composite_anom`, `tci`, `vci`) start being statistically meaningful.
Any shorter and the model is mostly fed `train_medians.json` imputations.
Any longer and the first ingest stops fitting comfortably in a laptop's
disk + runtime budget. The customer-handoff config can keep production at
1100/36 if their hardware allows.

### 3. Disabled datasets the 3 model schemes don't use

Looking at the actual `feature_names.json` for each of the three trained
models (biannual, quadseasonal, monthly), only 7 satellite datasets feed
features that any model consumes:

- `modis-ndvi`, `modis-lst-day`, `modis-sr` (NIR + SWIR1 only),
  `chirps-rainfall`, `era5-soil-moisture` — temporal
- `jrc-water` (occurrence asset only), `worldcover` — static

The other 7 datasets that ship enabled in `main` (`modis-evi`,
`modis-lai`, `modis-fpar`, `modis-gpp`, `modis-et-8day`, `modis-pet-8day`,
`modis-lst-night`) are flipped off because they're pure waste — ingest
fetches them, COGs land in S3, and nothing reads them.
`feature_extract.py:DEFAULT_SKIP_COLLECTIONS` was extended to match so
feature extraction doesn't crash trying to open parquets for the disabled
ones.

### 4. WorldCover at 100 m instead of 10 m

ESA WorldCover is published as 12 tiles at 10 m resolution. Merging them
across the Kenya bbox (~10 billion pixels) OOM-kills any container with
less than ~32 GB RAM. Bumped to `resolution_m: 100` in `datasets.yaml` —
which is still 200×200 pixels per 20 km feature window, plenty for
class-fraction features (`wc_water`, `wc_builtup`, `wc_cropland`). A
production deploy with bigger Fargate tasks could keep 10 m if desired,
but for a single laptop this is the sane default.

### 5. New: `infra/build_parquets.py` — fills a gap in `main`

Production reads NDVI/LST/SR/JRC/WorldCover parquets from a SageMaker
training bucket. The lmr-container's `parquet_bridge.py` only auto-builds
parquets for datasets that have a `parquet_bridge:` block — which today
is just `chirps-rainfall` and `era5-soil-moisture`. So a customer
deploying from scratch with no SageMaker bucket has no path to produce
parquets for the model's other 6 inputs.

`infra/build_parquets.py` closes that gap by reading raw COGs from MinIO
and producing wide-format parquets in the schema `ward_features.py`
expects. It also applies **MODIS scale factors** (`÷10000` for NDVI/SR,
`×0.02` for LST) which the training pipeline does upstream but the
lmr-container ingest never did. **Without this scaling, Ridge model
predictions land in the −800 range instead of the 0–0.2 range** — a real
gotcha that took a few rounds of debugging to track down. See "Bugs
discovered" below.

For static layers (JRC, WorldCover), the script also caps output at ~5 M
pixels via on-read decimation — same OOM concern as WorldCover above.

### 6. New: `docker-compose.local.yml`, `Dockerfile.frontend`, helper scripts

Thin tooling to make the laptop stack reproducible:

- `docker-compose.local.yml` defines the four services and threads the
  AWS env vars (both the boto3-style `AWS_ENDPOINT_URL` and the GDAL-style
  `AWS_S3_ENDPOINT` / `AWS_VIRTUAL_HOSTING=FALSE` / `AWS_HTTPS=NO`)
  through to all the right containers.
- `infra/local-bootstrap.sh` validates the bundle layout, then uploads it
  to MinIO via a one-shot `minio/mc` Docker container. **No host install
  of `mc` required** — that lets bootstrap run in a non-interactive shell
  without sudo.
- `infra/local-pipeline.sh` is the one-stop manual trigger
  (`ingest`/`parquets`/`features`/`infer`/`all`). Replaces production's
  EventBridge cron + Step Functions orchestrator.
- `Dockerfile.frontend` clones the pinned `prism-app` commit, injects
  `frontend/kenya_config/`, applies the existing patch, builds the dist,
  and serves via nginx. Mirrors what `infra/deploy-all.sh` Phase 4 does
  for AWS Amplify.

### 7. Frontend config patches

`frontend/kenya_config/layers.json`:
- CloudFront → `localhost:8000`, prod bucket → local bucket
- Raster `base_url` placeholders changed `{YYYY-MM-DD}` →
  `{YYYY_MM_DD}` so PRISM substitutes underscores matching our COG
  folder layout
- `dates: [...]` arrays refreshed from actual MinIO contents (no more
  stale 2018-2020 entries)
- Risk legend labels: `Normal` → `Normal (<5%)`,
  `Concerning` → `Concerning (5-10%)`, `Critical` → `Critical (>10%)`
  (matches `DEFAULT_RISK_THRESHOLDS` in postprocess.py)

`frontend/kenya_config/prism.json` — pruned `categories.hazards.*` to only
list layers backed by datasets we actually ingest.

`frontend/kenya_config/dashboard.json` — new file, contents `[]`. PRISM
fetches `/data/<country>/dashboard.json` on load. Without this file the
SPA fallback returns `index.html` and PRISM throws "Dashboard
configuration is not valid JSON". The validator schema is
`array(dashboardRowSchema)` — so the file must be an empty array, not an
object like `{"reports":[]}`.

### 8. Backend bug fixes

These are real bugs that affect production too. Two of them are filed as
PR #3 against `main`. The third is mostly local.

| Bug | Fix |
|---|---|
| `preprocess.py` `X_ridge` undefined (commit `d538304` accidentally deleted the Ridge scaling step) → every inference run crashes with `NameError` | Restored 2 lines. **Filed as [PR #3](https://github.com/ConorHuh/wfp-lmr-capstone/pull/3) against main.** |
| `routes.py` prediction endpoint had three issues: (a) only accepted underscore dates, PRISM sends hyphens; (b) looked in `predictions/livestock-mortality-{scheme}/{folder}/` while postprocess writes to `predictions/livestock-mortality/{date}/`; (c) hardcoded `_QUADSEASONAL` table only covered 2018–2020 | Simplified the route — drop scheme suffix, drop folder remapping, accept either separator. Same shape as what postprocess writes. |
| `ward_features.py` `compute_dem_roughness` crashes when `dem.parquet` doesn't exist (we don't ingest DEM since no model uses it) | Catches the `NoSuchKey`, returns NaN for `dem_std` / `dem_range` so downstream imputation handles it. |

---

## Run-time gotchas worth knowing

- **First ingest pulls a lot of data.** 24 months of MODIS NDVI + LST + SR
  + CHIRPS + ERA5 across Kenya is several hours. WorldCover and JRC are
  static one-time pulls. Watch progress with the MinIO console at
  http://localhost:9001 (minioadmin/minioadmin).

- **Some datasets lag.** UCSB CHIRPS has a ~2-month lag — ingest will get
  HTTP 404 on the most recent month or two and skip those gracefully.
  ERA5-Land has ~3-month lag and the ingest code throws an `IndexError`
  if you ask for an unpublished month — non-fatal; the run continues.

- **Re-running `features` doesn't pick up new parquet data.** The
  feature-extract step caches per-collection parquets at
  `s3://lmr-data-cogs-local/inference/ward_features_*/extracted/`. After
  changing parquet contents (e.g. fixing a scale factor in
  `build_parquets.py`), delete that prefix before re-running:
  ```bash
  docker run --rm --network wfp-lmr-capstone_lmr-net \
    -e MC_HOST_lmr=http://minioadmin:minioadmin@minio:9000 \
    --entrypoint mc minio/mc:latest \
    rm --recursive --force lmr/lmr-data-cogs-local/inference/
  ```

- **PRISM date picker uses the layer's `dates: [...]` array.** If you
  re-ingest fresh data later, the dates baked into `layers.json` will go
  stale. Re-run the date-refresh logic in
  `frontend/kenya_config/layers.json` (or rebuild the frontend image)
  to surface them in the UI.

---

## Troubleshooting

**"Dashboard configuration is not valid JSON" or "Invalid dashboard
configuration: root: Invalid input"** — `frontend/kenya_config/dashboard.json`
is missing or wrong shape. The schema is an array, not an object. File
content should be exactly `[]`.

**"Request failed for fetching admin level data at
http://localhost:8000/predictions/livestock-mortality-monthly/{YYYY_MM_DD}"**
— PRISM tried to fetch with no selected date. Either the layer's `dates`
array is empty, or none of its dates falls at-or-before "today". Check
the dates arrays in `layers.json`.

**Tile request returns transparent PNG (~334 bytes) for every zoom
level** — the tile is hitting an empty/edge area of the COG. Pan to a
populated area (Marsabit center: lat 2.5, lon 37.5) and zoom in.

**TiTiler logs `RasterioIOError: HTTP response code: 404` for s3:// URLs**
— GDAL's `/vsis3/` driver isn't honoring the MinIO endpoint override.
Check the lmr-serve container has all four env vars:
`AWS_ENDPOINT_URL`, `AWS_S3_ENDPOINT`, `AWS_VIRTUAL_HOSTING=FALSE`,
`AWS_HTTPS=NO`.

**Ridge predictions in the hundreds (not 0–0.2)** — `build_parquets.py`
isn't applying scale factors. Confirm the `TEMPORAL_TARGETS` and
`STATIC_TARGETS` tuples include the per-product `scale_factor` and
`nodata` values; rebuild parquets; clear the feature-extract cache
(see above); re-run features + infer.

**Inference crashes with `NameError: name 'X_ridge' is not defined`** —
this branch's `preprocess.py` already has the fix; if you're seeing it,
either you've reverted that file or you're on a different branch.

**Feature extraction crashes on missing parquet** — a dataset is
missing from `DEFAULT_SKIP_COLLECTIONS` in `feature_extract.py:15`. Add
its `COLLECTIONS` registry key (see
`backend/src/lmr/infer/ward_features.py:132`) — note these are the
short keys (`ndvi_250m`, `lst_day`, …), not the YAML names.

**`docker compose run` complains about missing `lmr-cli` profile** —
add `--profile cli`:
```bash
docker compose -f docker-compose.local.yml --profile cli run --rm lmr-cli ...
```
The pipeline script handles this internally; the gotcha is for ad-hoc runs.

**Ingest dies silently (container exits, no error in logs) during
WorldCover** — it OOM'd while merging tiles. Confirm
`worldcover.processing.resolution_m` is `100` in `datasets.yaml`, not
`10`. If you need 10 m, give Docker more memory or use a workstation with
≥ 32 GB.

---

## Going from local to AWS

This branch is **not deployable** via `infra/deploy-all.sh` — bucket
naming, CORS, and several backend assumptions diverge.

For a customer-handoff path, cherry-pick selectively:

- ✅ Take: the dataset prune list, the trimmed `modis-sr` asset list,
  the WorldCover-100m default, `feature_extract.py` skip-list updates,
  the `routes.py` simplification, and the `ward_features.py` DEM guard
- ✅ Take: `infra/build_parquets.py` (with scale factors!) — production
  needs this if customers don't have a SageMaker training bucket
- ❌ Leave behind: bucket name swaps, lookback bumps, CORS restrictions,
  `docker-compose.local.yml`, `Dockerfile.frontend`, the local helper
  scripts

PR #3 is the one fix that should land in `main` immediately —
`preprocess.py X_ridge`. Without it production inference also crashes,
silently, on every Step Function run.
