# LMR Data Platform — Architecture

Early warning system for livestock mortality in Kenya's Marsabit County. Ingests MODIS satellite imagery from Microsoft Planetary Computer, serves Cloud Optimized GeoTIFFs as map tiles, runs ward-level ensemble inference via Step Functions, and displays mortality risk predictions through WFP's Prism frontend.

## System Diagram

```
                                    ┌─────────────────────┐
                                    │  Planetary Computer  │
                                    │  (STAC Catalog)      │
                                    └────────┬────────────┘
                                             │ STAC search + download
                                             ▼
┌──────────────┐  every 10 days   ┌─────────────────────┐
│  EventBridge │ ───────────────► │  Fargate: Ingest    │
│  (cron)      │                  │  --mode ingest      │
└──────────────┘                  └────────┬────────────┘
                                           │ COGs + zonal stats + manifest
                                           ▼
                                  ┌─────────────────────┐
                                  │  S3: lmr-data-cogs  │
                                  │  ├── ingested/      │
                                  │  ├── stats/         │
                                  │  ├── manifests/     │
                                  │  ├── models/        │
                                  │  ├── inference/     │
                                  │  └── predictions/   │
                                  └────────┬────────────┘
                                           │
                          ┌────────────────┼────────────────┐
                          │                │                │
                          ▼                ▼                ▼
                ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
                │ EventBridge  │  │ Fargate:     │  │ Step Functions   │
                │ (manifest    │  │ Serve API    │  │ (conditional)    │
                │  trigger)    │  │ /health      │  │                  │
                └──────┬───────┘  │ /collections │  │ FeatureExtract   │
                       │          │ /predictions │  │   ↓              │
                       ▼          └──────┬───────┘  │ Parallel:        │
                ┌──────────────┐         │          │  biannual        │
                │ Lambda       │         │          │  quadseasonal    │
                │ (SFN trigger)│         │          │  monthly         │
                └──────┬───────┘  ┌──────┴───────┐  └──────────────────┘
                       │          │ TiTiler      │
                       ▼          │ (mounted     │
                Step Functions    │  at /cog)    │
                Inference         └──────────────┘
                Pipeline                 │
                                         ▼
                                  ┌─────────────────────────────┐
                                  │  ALB (HTTP:80 → :8000)      │
                                  └──────────┬──────────────────┘
                                             │
                                             ▼
                                  ┌─────────────────────────────┐
                                  │  CloudFront (HTTPS + CORS)   │
                                  └──────────┬──────────────────┘
                                             │
                                             ▼
                                  ┌─────────────────────────────┐
                                  │  Amplify: Prism Frontend     │
                                  │  (Kenya config)              │
                                  └─────────────────────────────┘
```

## Container

Single Docker image (`backend/`) with four runtime modes:

| Mode | Command | Resources | Purpose |
|------|---------|-----------|---------|
| `ingest` | `lmr --mode ingest --config config/datasets.yaml` | 1 vCPU / 4 GB | Pull satellite data from Planetary Computer, convert to COGs, upload to S3 |
| `serve` | `lmr --mode serve --config config/datasets.yaml` | 2 vCPU / 8 GB | FastAPI + TiTiler tile server for Prism |
| `feature-extract` | `lmr --mode feature-extract --time-start YYYY-MM --time-end YYYY-MM` | 4 vCPU / 16 GB | Ward-level satellite feature extraction for inference |
| `infer` | `lmr --mode infer --scheme biannual\|quadseasonal\|monthly` | 1 vCPU / 4 GB | Ensemble inference for one season scheme |

Built on `python:3.11-slim` with GDAL. Dependencies managed by `uv`. Image must be built with `--platform linux/amd64` for Fargate (even from Apple Silicon).

### Source Layout

```
backend/src/lmr/
├── cli.py              # Entrypoint — parses --mode, --config, --scheme, --time-start/end
├── config.py           # Pydantic models for datasets.yaml
├── common/
│   ├── s3.py           # Shared boto3 S3 client
│   └── logging.py      # Structured logging
├── ingest/
│   ├── stac_client.py  # STAC search against Planetary Computer
│   ├── cog.py          # Download, clip, reproject, COG conversion
│   ├── zonal.py        # Per-ward zonal statistics
│   └── s3.py           # Upload COGs + manifests, date tracking
├── serve/
│   ├── app.py          # FastAPI app, CORS, TiTiler mount
│   ├── routes.py       # API endpoints (health, collections, predictions, tiles)
│   ├── s3.py           # Presigned URLs, S3 helpers
│   └── titiler_setup.py # TiTiler COG tiler factory
└── infer/
    ├── feature_extract.py  # CLI entry point for feature extraction
    ├── ward_features.py    # 1000+ line feature engineering pipeline
    ├── pipeline.py         # Orchestrates preprocess → ensemble → postprocess
    ├── preprocess.py       # Impute NaNs, scale features for Ridge
    ├── ensemble.py         # 4-model ensemble + monthly stacked meta-learner
    └── postprocess.py      # Risk levels, GeoJSON/GeoTIFF/CSV output
```

## Inference Pipeline

Opt-in via `inference.enabled: true` in `datasets.yaml`. When enabled, a Step Functions state machine is deployed and triggered automatically after each ingest run.

### Trigger Flow

1. Ingest task writes `manifests/ingest-{ts}.json` to S3
2. S3 EventBridge notification fires on `manifests/` prefix
3. Lambda computes time window (today − `feature_window_months` months → today)
4. Lambda starts Step Functions execution with time window + infrastructure config

### Pipeline Steps

| Step | Mode | Resources | Input | Output |
|------|------|-----------|-------|--------|
| Feature Extraction | `feature-extract` | 4 vCPU / 16 GB | Satellite parquets from source bucket | `inference/ward_features_*/ward_features_{scheme}.parquet` |
| Inference (×3 parallel) | `infer --scheme {scheme}` | 1 vCPU / 4 GB | Ward features parquet + model bundle | `predictions/livestock-mortality/{YYYY_MM_DD}/ward_predictions.{csv,geojson,tif}` |

### Ensemble Model

Each scheme has a self-contained model bundle in `s3://lmr-data-cogs-{env}/models/inference_bundle/{scheme}/`:

| File | Purpose |
|------|---------|
| `xgboost_model.joblib` | XGBoost model |
| `lgbm_model.joblib` | LightGBM model |
| `rf_model.joblib` | Random Forest model |
| `ridge_model.joblib` | Ridge regression model |
| `ensemble_weights.json` | Per-model weights |
| `feature_names.json` | Ordered feature columns |
| `train_medians.json` | Per-feature medians for NaN imputation |
| `feature_scaler.joblib` | StandardScaler for Ridge inputs |
| `run_metadata.json` | Training label mean for risk thresholds |

Monthly scheme additionally includes: `meta_model.joblib`, `meta_scaler.joblib`, `meta_feature_names.json`, `ward_encoding.json` (stacked meta-learner).

**Revert path:** Model artifacts were copied from the SageMaker training bucket. Originals remain at `s3://amazon-sagemaker-575108933641-us-east-1-c422b90ce861/dzd-.../inference_bundle/`. To revert, change `model_s3_prefix` in `datasets.yaml` to the fallback path and redeploy.

### WFP Toggle

Set `inference.enabled: false` in `datasets.yaml` → deploy script passes `EnableInferencePipeline=false` → Step Functions stack, Lambda trigger, and infer task definitions are not created. The container image is identical regardless.

## S3 Bucket Structure

Bucket: `lmr-data-cogs-dev`

```
ingested/
  modis-ndvi/2026_01_17/250m_16_days_NDVI.tif
  ...
stats/
  modis-ndvi/2026_01_17/ward_stats.parquet
  ...
manifests/
  ingest-2026-01-17T00:00:00Z.json
models/
  inference_bundle/
    biannual/       # 9 model files
    quadseasonal/   # 9 model files
    monthly/        # 13 model files (includes meta-learner)
  geoBoundaries-KEN-ADM3.geojson
inference/
  ward_features_2023-04_2026-04/
    ward_features_biannual.parquet
    ward_features_quadseasonal.parquet
    ward_features_monthly.parquet
  preprocessed/{scheme}/
    preprocessed_features.parquet
    preprocessed_features_ridge.parquet
    inference_metadata.parquet
    predictions_with_metadata.parquet
predictions/
  livestock-mortality/
    2019_12_31/               # biannual OND 2019
      ward_predictions.csv
      ward_predictions.geojson
      ward_predictions.tif    # 3-band COG (risk, confidence, SHAP importance)
    2020_05_31/               # biannual MAM 2020
      ...
```

Date folders use **underscore format** (`YYYY_MM_DD`) to match Prism's `{YYYY_MM_DD}` date template. Season labels are mapped to end-of-season dates (e.g., OND → `{year}_12_31`).

## Ingestion Pipeline

Triggered every 10 days by EventBridge. For each enabled dataset in `backend/config/datasets.yaml`:

1. **STAC search** — query Planetary Computer for new imagery since last ingested date
2. **Download + process** — clip to Kenya bbox, reproject to EPSG:4326, convert to COG (tiled 256x256, DEFLATE compression, overviews)
3. **Zonal stats** — compute per-ward statistics using `kenya_wards.geojson` boundary (12 Marsabit wards)
4. **Upload** — write COG + stats to S3, record manifest

Supports `--start-date` and `--end-date` overrides for backfills. Incremental by default (resumes from last ingested date per dataset).

### Enabled Datasets

| Dataset | Collection | Resolution | Cadence |
|---------|-----------|------------|---------|
| MODIS NDVI | modis-13Q1-061 | 250m | 16-day |
| MODIS EVI | modis-13Q1-061 | 250m | 16-day |
| MODIS LAI | modis-15A2H-061 | 500m | 8-day |
| MODIS FPAR | modis-15A2H-061 | 500m | 8-day |
| MODIS LST Day | modis-11A2-061 | 1km | 8-day |
| MODIS LST Night | modis-11A2-061 | 1km | 8-day |
| MODIS SR (4 bands) | modis-09A1-061 | 500m | 8-day |
| MODIS ET | modis-16A3GF-061 | 500m | Annual |
| MODIS PET | modis-16A3GF-061 | 500m | Annual |
| MODIS GPP | modis-17A2HGF-061 | 500m | 8-day |

## Serve API

FastAPI application with TiTiler mounted at `/cog`. Runs on Fargate behind ALB + CloudFront.

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | ALB health check |
| `GET /collections` | List datasets with available dates |
| `GET /raster_geotiff?collection=&date=&asset=` | Presigned S3 URL for COG download |
| `GET /tile_url?collection=&date=&asset=` | TiTiler tile URL template for Prism |
| `GET /latest?model=livestock-mortality` | Latest prediction COG presigned URL |
| `GET /predictions/livestock-mortality/dates` | Available prediction dates |
| `GET /predictions/livestock-mortality/{date}` | Ward prediction data for Prism tooltips |
| `GET /cog/tiles/WebMercatorQuad/{z}/{x}/{y}` | TiTiler raster tile serving |

### Prediction Ward Data

`GET /predictions/livestock-mortality/{date}` reads `ward_predictions.geojson` from S3, extracts `pcode` directly from the GeoJSON properties (or falls back to `_WARD_PCODE_MAP` for legacy files), flattens `top_features` arrays, and returns `{"DataList": [...]}` for Prism's `admin_level_data` layer type.

## Prism Frontend

WFP's open-source Prism platform, deployed to AWS Amplify with Kenya-specific configuration. Configuration lives in `frontend/kenya_config/`. The Prism source is not checked in — it is cloned at a pinned commit during deployment by `infra/deploy-all.sh`.

### Configuration Files

```
frontend/kenya_config/
├── prism.json                  # Country settings, map center, layer categories
├── layers.json                 # Layer definitions (types, URLs, legends, dates)
└── admin_boundaries.geojson    # Ward boundaries with pcode, first_prov, first_dist
```

### Layer Types

**`static_raster`** — COG tiles served via TiTiler. URL contains `{YYYY-MM-DD}` date template.

**`admin_level_data`** — Fetches JSON from a URL, joins to boundary polygons by `pcode`, colors wards by a numeric `data_field`, shows tooltip on click via `feature_info_props`. URL contains `{YYYY_MM_DD}` date template.

## Infrastructure

All CloudFormation templates are in `infra/cloudformation/`. Root stack: `lmr-platform-{env}` (`infra/cloudformation/main.yaml`).

| Nested Stack | Template | Resources |
|-------------|----------|-----------|
| ECR | `ecr.yaml` | Container registry |
| S3 | `s3.yaml` | Data bucket with EventBridge notifications |
| IAM | `iam.yaml` | Task, execution, EventBridge, Step Functions, Lambda roles |
| Fargate Ingest | `fargate-ingest.yaml` | Ingest task definition, ECS cluster |
| EventBridge | `eventbridge.yaml` | Scheduled ingest trigger (every 10 days) |
| Fargate Serve | `fargate-serve.yaml` | Serve task definition, ECS service, ALB |
| CloudFront | `cloudfront.yaml` | HTTPS distribution with CORS, cache |
| Amplify | `amplify.yaml` | Frontend app and main branch |
| Fargate Infer | `fargate-infer.yaml` | Feature-extract + infer task definitions (conditional) |
| Step Functions | `step-functions.yaml` | State machine, Lambda trigger, EventBridge rule (conditional) |

Fargate Infer and Step Functions stacks are only deployed when `EnableInferencePipeline=true`.

### Fresh-Account Deployment

The entire platform deploys from a fresh AWS account:

```bash
./infra/deploy-all.sh
```

The script automatically:
- Creates a CFN artifacts S3 bucket (bootstrap)
- Auto-discovers the default VPC and public subnets
- Reads `inference.enabled` from `datasets.yaml` → passes to CloudFormation
- Builds and pushes the Docker image
- Deploys all CloudFormation stacks
- Migrates model artifacts (when inference enabled)
- Invalidates CloudFront cache
- Builds and deploys Prism frontend to Amplify

Override VPC with `--vpc-id` and `--subnet-ids` flags. See `./infra/deploy-all.sh --help` for all options.

## AWS Resources

| Resource | ID / Name |
|----------|-----------|
| ECS Cluster | `lmr-cluster-{env}` |
| S3 Bucket | `lmr-data-cogs-{env}` |
| ECR Repository | `lmr-container-{env}` |
| CloudFront | Managed by `cloudfront.yaml` (ID in stack outputs) |
| Amplify | Managed by `amplify.yaml` (ID in stack outputs) |
| Step Functions | `lmr-ward-inference-{env}` (when inference enabled) |
| CloudWatch Logs | `/ecs/lmr-ingest-{env}`, `/ecs/lmr-serve-{env}`, `/ecs/lmr-feature-extract-{env}`, `/ecs/lmr-infer-{env}` |

## Key Design Decisions

1. **Single container, multiple modes** — reduces image sprawl and keeps shared code (config, S3 helpers) in one place.
2. **TiTiler as library** — mounted directly into FastAPI, avoids running a separate tile server. Reads COGs from S3 via GDAL `/vsis3/` using Fargate task role credentials.
3. **Underscore date format in S3** — matches Prism's `{YYYY_MM_DD}` template natively. Season labels are mapped to end-of-season dates for compatibility.
4. **pcode as join key** — admin boundaries and prediction data both use pcode for reliable matching.
5. **Step Functions replaces SageMaker** — inference orchestration via Step Functions + ECS Fargate. Conditional deployment: `inference.enabled: false` deploys no inference infrastructure. Saves ~$1,000/year vs SageMaker Pipelines.
6. **Model artifacts loaded from S3** — ensemble model bundles are stored in the data bucket, not baked into the Docker image. Revert path documented via `model_s3_prefix_fallback` in config.
7. **COG format everywhere** — Cloud Optimized GeoTIFFs with tiled layout (256x256), overviews, and DEFLATE compression for efficient HTTP range reads.
8. **CloudFront in CloudFormation** — HTTPS proxy for ALB, managed declaratively. CORS (Origin header forwarding, OPTIONS) configured in template, not manual API calls.
9. **Default VPC** — deploy script auto-discovers the default VPC to avoid the cost of a managed VPC with NAT gateways. Override with `--vpc-id` for custom VPCs.
10. **Prism cloned at deploy time** — Prism source is not checked into this repo. `deploy-all.sh` clones a pinned commit, injects `frontend/kenya_config/`, applies patches, builds, and deploys to Amplify.
