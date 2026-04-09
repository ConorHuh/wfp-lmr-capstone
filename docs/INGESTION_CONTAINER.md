# Ingestion Container — How It Works

This doc explains the `lmr-container/` component of the LMR platform for teammates who need to understand what it does, how data flows through it, and how to interact with it.

## What it does

The ingestion container pulls satellite imagery from Microsoft's Planetary Computer (a public STAC catalog), converts it to Cloud Optimized GeoTIFFs (COGs), computes per-ward statistics, and uploads everything to S3. It runs on AWS Fargate on a recurring schedule (every 10 days) and is also the same container that will serve data to the Prism frontend (Phase 2).

## Data flow

```
Planetary Computer (STAC API)
        │
        │  1. Search for satellite imagery covering all of Kenya
        │     (Sentinel-2, CHIRPS rainfall, etc.)
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
├── stats/             ← per-ward summary statistics (JSON)
└── manifests/         ← what was ingested and when
        │
        ▼
S3 Event Notification triggers SageMaker Pipeline
        │
        ▼
S3: predictions/       ← ML model output COGs
```

## Key design decisions

### Country-wide ingestion, config-driven analysis

The container always pulls satellite data for **all of Kenya**. It does not filter by county or ward at ingestion time. This means:

- You never need to re-ingest data when adding a new region of interest
- The COGs in S3 cover the full country and can be used by any downstream process
- Ward-level filtering only happens during the zonal stats step, controlled by `config/datasets.yaml`

### Adding a new county or region

Edit `config/datasets.yaml` and add to the `admin_levels` filter:

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

Append a block to the `datasets` list in `config/datasets.yaml`:

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
├── ingested/ndvi-sentinel2/2026-03-09/B04.tif    ← raw satellite band (full Kenya COG)
├── ingested/ndvi-sentinel2/2026-03-09/B08.tif
├── ingested/rainfall-chirps/2026-03-05/precip.tif
│
├── stats/ndvi-sentinel2/2026-03-09/admin3_B04.json  ← per-ward zonal stats
│   # Contains: [{id: "KE0842", name: "Marsabet central", stats: {mean, median, min, max, ...}}, ...]
│
├── predictions/livestock-mortality/2026-03-09/prediction.tif  ← from SageMaker (your output)
│
└── manifests/ingest-2026-03-09T00:00:00Z.json     ← log of what was ingested
```

## For the SageMaker pipeline team

Your pipeline is triggered automatically when a new manifest appears in `manifests/`. The manifest JSON tells you exactly what was ingested:

```json
{
  "run_id": "ingest-2026-03-09T00:00:00Z",
  "timestamp": "2026-03-09T01:23:45Z",
  "datasets_processed": [
    {
      "name": "ndvi-sentinel2",
      "items_ingested": 2,
      "s3_keys": ["ingested/ndvi-sentinel2/2026-03-09/B04.tif", "..."],
      "stats_keys": ["stats/ndvi-sentinel2/2026-03-09/admin3_B04.json", "..."],
      "stac_items": ["S2A_MSIL2A_20260309..."]
    }
  ],
  "status": "success"
}
```

**Your output contract:** Write prediction COGs to:
```
s3://lmr-data-cogs-dev/predictions/{model-name}/{date}/prediction.tif
```

See `docs/PLAN_SAGEMAKER_PIPELINE.md` for the full integration spec.

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
| Log Group | `/ecs/lmr-ingest-dev` |

## Running it manually

```bash
# Trigger a manual ingest (incremental — only new data)
aws ecs run-task \
  --cluster lmr-cluster-dev \
  --task-definition lmr-ingest-dev \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0dad0b63d1d403190],assignPublicIp=ENABLED}" \
  --region us-east-1

# Trigger a full historical ingest
aws ecs run-task \
  --cluster lmr-cluster-dev \
  --task-definition lmr-ingest-dev \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0dad0b63d1d403190],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"lmr-container","command":["--mode","ingest","--config","/app/config/datasets.yaml","--full-history"]}]}' \
  --region us-east-1
```

## Local development

```bash
cd lmr-container
uv sync                                              # install deps
uv run pytest -v                                     # run tests
uv run lmr --mode ingest --config config/datasets.yaml  # run locally
```
