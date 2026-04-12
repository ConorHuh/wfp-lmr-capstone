# Ingestion Container — How It Works

This doc explains the `backend/` component of the LMR platform for teammates who need to understand what it does, how data flows through it, and how to interact with it.

## What it does

The backend container has four runtime modes, all packaged in a single Docker image:

| Mode | Purpose |
|------|---------|
| `ingest` | Pull satellite data from Planetary Computer, convert to COGs, upload to S3 |
| `serve` | FastAPI + TiTiler tile server for the Prism frontend |
| `feature-extract` | Ward-level satellite feature extraction for inference |
| `infer` | Ensemble inference for one season scheme |

The container runs on AWS Fargate. Ingestion is triggered every 10 days by EventBridge. After ingestion, a Step Functions pipeline runs feature extraction + inference automatically (when inference is enabled).

## Data flow

```
Planetary Computer (STAC API)
        │
        │  1. Search for satellite imagery covering all of Kenya
        │     (MODIS vegetation, temperature, reflectance)
        │
        ▼
┌─────────────────────────────────┐
│  Ingest Container (Fargate)     │
│                                 │
│  2. Download raster assets      │
│  3. Clip to Kenya bounding box  │
│  4. Reproject → COG format      │
│  5. Compute zonal stats for     │
│     each ward in config         │
│  6. Upload COGs + stats to S3   │
│  7. Write manifest JSON         │
└─────────────────────────────────┘
        │
        ▼
S3: lmr-data-cogs-dev/
├── ingested/          ← full Kenya satellite COGs
├── stats/             ← per-ward summary statistics (parquet)
└── manifests/         ← what was ingested and when
        │
        ▼  (S3 event notification → Lambda → Step Functions)
        │
┌─────────────────────────────────┐
│  Feature Extract (Fargate)      │
│  --mode feature-extract         │
│  4 vCPU / 16 GB                 │
│                                 │
│  Ward-level satellite feature   │
│  engineering for all schemes    │
└─────────────────────────────────┘
        │
        ▼  (3 parallel branches)
┌─────────────────────────────────┐
│  Infer (Fargate) × 3 parallel   │
│  --mode infer --scheme {scheme}  │
│  1 vCPU / 4 GB each             │
│                                  │
│  Ensemble model predictions:     │
│  • biannual                      │
│  • quadseasonal                  │
│  • monthly                       │
└─────────────────────────────────┘
        │
        ▼
S3: predictions/livestock-mortality/{YYYY_MM_DD}/
├── ward_predictions.csv
├── ward_predictions.geojson
└── ward_predictions.tif    ← 3-band COG (risk, confidence, SHAP)
        │
        ▼
┌─────────────────────────────────┐
│  Serve Container (Fargate)       │
│  --mode serve                    │
│  FastAPI + TiTiler → ALB →       │
│  CloudFront (HTTPS) → Prism      │
└─────────────────────────────────┘
```

## Key design decisions

### Country-wide ingestion, config-driven analysis

The container always pulls satellite data for **all of Kenya**. It does not filter by county or ward at ingestion time. This means:

- You never need to re-ingest data when adding a new region of interest
- The COGs in S3 cover the full country and can be used by any downstream process
- Ward-level filtering only happens during the zonal stats step, controlled by `config/datasets.yaml`

### Adding a new county or region

Edit `backend/config/datasets.yaml` and add to the `admin_levels` filter:

```yaml
admin_levels:
  - level: 3
    name: "wards"
    boundary_file: "boundaries/kenya_wards.geojson"
    id_field: "pcode"
    name_field: "iebc_wards"
    filter:
      field: "first_dist"
      values: ["Marsabit", "Turkana"]  # ← just add here
```

No code changes, no re-ingestion. The next run computes stats for the new wards automatically.

### Adding a new dataset

Append a block to the `datasets` list in `backend/config/datasets.yaml`:

```yaml
datasets:
  # ... existing datasets ...
  - name: "soil-moisture"
    enabled: true
    collection: "some-stac-collection-id"
    assets: ["sm"]
    temporal:
      lookback_days: 16
    processing:
      output_format: "cog"
      resolution_m: 1000
      crs: "EPSG:4326"
    s3_key_template: "{prefix}/{dataset}/{date}/{asset}.tif"
```

### Incremental ingestion

After the first run, the container only pulls **new data** since the last ingestion. It checks S3 for the latest date folder per dataset and sets the STAC search window accordingly. Use `--full-history` to override this and pull everything.

## What's in S3

```
lmr-data-cogs-dev/
│
├── ingested/modis-ndvi/2026_01_17/250m_16_days_NDVI.tif   ← satellite COG (full Kenya)
├── ingested/modis-lai/2026_01_25/Lai_500m.tif
│
├── stats/modis-ndvi/2026_01_17/admin3_250m_16_days_NDVI.json  ← per-ward zonal stats
│
├── models/inference_bundle/biannual/    ← trained ensemble model artifacts
├── models/inference_bundle/monthly/
├── models/inference_bundle/quadseasonal/
│
├── inference/ward_features_*/           ← feature extraction outputs
│
├── predictions/livestock-mortality/2019_12_31/   ← biannual OND 2019
│   ├── ward_predictions.csv
│   ├── ward_predictions.geojson
│   └── ward_predictions.tif             ← 3-band COG
│
└── manifests/ingest-2026-03-09T00:00:00Z.json   ← ingestion log
```

## Current ward coverage

The config currently computes stats for **12 wards in Marsabit County**:

| Ward | PCode |
|------|-------|
| Dukana | KE0212 |
| Illeret | KE0360 |
| Karare | KE0505 |
| Kargi/South Horr | KE0509 |
| Korr/Ngurunit | KE0677 |
| Logologo | KE0740 |
| Loiyangalani | KE0743 |
| Maikona | KE0796 |
| Marsabet Central | KE0842 |
| North Horr | KE1086 |
| Sagante/Jaldessa | KE1192 |
| Turbi | KE1338 |

The full boundary file contains all 1,425 Kenya wards (admin level 3).

## AWS resources

All deployed via CloudFormation (`lmr-platform-dev` stack):

| Resource | Name |
|----------|------|
| ECS Cluster | `lmr-cluster-dev` |
| S3 Bucket | `lmr-data-cogs-dev` |
| ECR Repository | `lmr-container-dev` |
| EventBridge Rule | `lmr-ingest-schedule-dev` (every 10 days) |
| Step Functions | `lmr-ward-inference-dev` (when inference enabled) |
| CloudFront | HTTPS proxy for tile server |
| Amplify | Prism frontend hosting |
| Log Groups | `/ecs/lmr-ingest-dev`, `/ecs/lmr-serve-dev`, `/ecs/lmr-feature-extract-dev`, `/ecs/lmr-infer-dev` |

## Running it manually

```bash
# Trigger a manual ingest (incremental — only new data)
aws ecs run-task \
  --cluster lmr-cluster-dev \
  --task-definition lmr-ingest-dev \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],assignPublicIp=ENABLED}" \
  --region us-east-1

# Trigger a full historical ingest
aws ecs run-task \
  --cluster lmr-cluster-dev \
  --task-definition lmr-ingest-dev \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"lmr-container","command":["--mode","ingest","--config","/app/config/datasets.yaml","--full-history"]}]}' \
  --region us-east-1
```

## Local development

```bash
cd backend
uv sync                                              # install deps
uv run pytest -v                                     # run tests
uv run lmr --mode ingest --config config/datasets.yaml  # run locally
uv run lmr --mode serve --config config/datasets.yaml   # run tile server locally
```
