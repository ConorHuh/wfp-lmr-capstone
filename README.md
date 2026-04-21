# LMR Data Platform — Livestock Mortality Risk Early Warning System

An end-to-end satellite-driven platform that predicts livestock mortality risk for pastoral communities in Marsabit County, Kenya. Built for the World Food Programme (WFP) as a UC Berkeley MIDS Capstone project (Spring 2026).

The system ingests MODIS satellite imagery on a recurring schedule, computes ward-level features, runs ensemble ML predictions, and delivers results through WFP's [Prism](https://github.com/WFP-VAM/prism-app) geospatial frontend — all deployed on AWS from a single command.

## What Users See

The platform is accessed through a Prism web application where users can:

- **Browse satellite data layers** — NDVI, EVI, LAI, FPAR, GPP vegetation indices; daytime and nighttime land surface temperature; surface reflectance bands. Each layer has a date picker to view historical imagery.
- **View livestock mortality predictions** — Ward-level risk maps showing predicted loss ratios as colored choropleth overlays, with Normal/Concerning/Critical classifications.
- **Inspect ward-level details** — Click any ward to see the predicted loss ratio, model confidence score, risk classification, and the top 5 satellite features driving the prediction (with SHAP importance values).
- **Access raster tiles** — All satellite data and predictions are served as Cloud Optimized GeoTIFF (COG) tiles through TiTiler, enabling smooth pan/zoom at any scale.

## How It Works

```
Every 10 days (EventBridge):

  Planetary Computer ──┐
  (MODIS NDVI, LST…)   ├──► Fargate: ingest ──► S3
  NASA Earthdata ──────┘    │                  ├── ingested/    (COGs)
  (8-day ET/PET)            │
                           │                  ├── stats/       (zonal stats)
                           └──────────────────├── manifests/   (run logs)
                                              │
                              S3 manifest event triggers Lambda
                                              │
                                              ▼
                                    Step Functions pipeline
                                    ├─ feature-extract (4 vCPU / 16 GB)
                                    │    ward-level satellite features
                                    └─ 3× parallel inference (1 vCPU / 4 GB each)
                                         ��─ biannual    ─┐
                                         ├─ quadseasonal ─┼──► predictions/
                                         └─ monthly      ─┘    (CSV + GeoJSON + COG)

  Prism frontend ◄── CloudFront ◄���─ ALB ◄── Fargate: serve
  (AWS Amplify)       (HTTPS)                (FastAPI + TiTiler, always-on)
```

### Pipeline Components

**Ingestion** — Queries Microsoft's Planetary Computer STAC catalog and NASA Earthdata (CMR STAC) for MODIS satellite imagery covering all of Kenya. Most datasets come from Planetary Computer; 8-day ET/PET (MOD16A2GF.061) comes from NASA Earthdata since the collection was deprecated from PC after July 2023. Downloads raster assets (including HDF-EOS2 extraction for NASA sources), clips to the Kenya bounding box, reprojects to EPSG:4326, converts to COG format (tiled 256x256, DEFLATE compression, overviews), computes per-ward zonal statistics for the 12 Marsabit wards, and uploads everything to S3. Incremental by default — only pulls data newer than the last run.

**Feature Extraction** — Reads stored satellite parquets and engineers ward-level features: vegetation indices, land surface temperature, surface reflectance, lagged values (1-3 month), drought composites (VCI/TCI/VHI), and temporal encodings. Uses a 3x3 grid sampling strategy within each ward polygon for spatial representativeness.

**Inference** — Loads pre-trained ensemble models from S3 and produces risk predictions for each ward across three temporal schemes. Each scheme has four base models (XGBoost, LightGBM, Random Forest, Ridge) combined by weighted averaging. The monthly scheme uses an additional stacked meta-learner. Outputs include risk levels, confidence scores, and SHAP feature importance.

**Serve** — An always-on FastAPI application with TiTiler mounted at `/cog` for serving COG tiles. Provides structured JSON endpoints for prediction data that Prism consumes. Runs behind ALB + CloudFront for HTTPS.

**Frontend** — WFP's open-source Prism platform (React + MapLibre + Redux) with Kenya-specific configuration. Not checked into this repo — cloned at a pinned commit during deployment, configured with our layers/boundaries, patched, built, and deployed to AWS Amplify.

### Temporal Schemes

The model predicts livestock mortality at three temporal granularities:

| Scheme | Periods/Year | Seasons | Use Case |
|--------|-------------|---------|----------|
| Biannual | 2 | LRLD (Mar-Sep), SRSD (Oct-Feb) | Strategic early warning |
| Quadseasonal | 4 | LRS, LRS_dry, SRS, SRS_dry | Mid-range planning |
| Monthly | 12 | Jan-Dec | Tactical/operational |

## Quick Start

### Prerequisites

- AWS CLI v2 (configured with credentials)
- Docker
- Node.js 20 (via nvm) + yarn
- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- NASA Earthdata account (free) — required for 8-day ET/PET ingestion. See [NASA Earthdata Setup](#nasa-earthdata-setup) below.

### Deploy Everything

```bash
./infra/deploy-all.sh
```

This single command handles the full deployment:
- Creates a CloudFormation artifacts bucket (bootstrap)
- Auto-discovers the default VPC and public subnets
- Builds the Docker image (`--platform linux/amd64` for Fargate)
- Deploys all CloudFormation stacks
- Pushes the container image to ECR and updates ECS
- Migrates model artifacts from the SageMaker training bucket (when inference enabled)
- Clones Prism at a pinned commit, injects Kenya config, builds, and deploys to Amplify
- Invalidates the CloudFront cache

```bash
./infra/deploy-all.sh --skip-frontend    # backend only
./infra/deploy-all.sh --skip-backend     # frontend only
./infra/deploy-all.sh --skip-build       # reuse existing Docker image
./infra/deploy-all.sh --help             # all options
```

### Local Development

```bash
cd backend
uv sync --group dev          # install dependencies
uv run pytest tests/ -v      # run tests (54 tests)
uv run lmr --mode serve --config config/datasets.yaml   # tile server on :8000
uv run lmr --mode ingest --config config/datasets.yaml   # satellite ingestion
```

### NASA Earthdata Setup

The platform ingests 8-day MODIS ET/PET data (MOD16A2GF.061) from NASA Earthdata. This collection was deprecated from Planetary Computer after July 2023, so NASA's CMR STAC catalog is used instead.

**1. Create a free NASA Earthdata account**

Sign up at https://urs.earthdata.nasa.gov — just an email and password. No institution or payment required.

**2. For local development**, set environment variables before running ingestion:

```bash
export EARTHDATA_USERNAME="your_earthdata_username"
export EARTHDATA_PASSWORD="your_earthdata_password"
uv run lmr --mode ingest --config config/datasets.yaml
```

**3. For AWS deployment**, store credentials in Secrets Manager (one-time setup per environment):

```bash
aws secretsmanager create-secret \
  --name lmr-earthdata-dev \
  --secret-string '{"username":"your_earthdata_username","password":"your_earthdata_password"}' \
  --region us-east-1
```

The Fargate ingest task automatically reads these credentials at runtime. The deploy script handles the IAM permissions.

**Why NASA Earthdata?** The 8-day gap-filled ET/PET product provides sub-monthly evapotranspiration data critical for features like `et_deficit_roll3_mean`. The annual composite previously available on Planetary Computer was too coarse — ET/PET values were constant across the year, making temporal features (lags, rolling means) ineffective.

## Repository Structure

```
backend/                    # Single Docker container (Python 3.11 + GDAL)
  src/lmr/
    cli.py                  #   CLI: --mode ingest|serve|infer|feature-extract
    config.py               #   Pydantic config models
    ingest/                 #   STAC search, COG conversion, zonal stats, S3 upload
    serve/                  #   FastAPI + TiTiler app, API routes, presigned URLs
    infer/                  #   Feature extraction, preprocessing, ensemble, postprocessing
    common/                 #   Shared S3 client, structured logging
  config/
    datasets.yaml           #   All platform config (datasets, inference toggle, S3 paths)
    boundaries/             #   Kenya ward boundaries GeoJSON (1,425 wards)
  tests/                    #   pytest tests
  Dockerfile                #   Multi-stage build with uv

frontend/                   # Prism frontend configuration (injected at deploy time)
  kenya_config/
    prism.json              #   App settings, map center, layer categories
    layers.json             #   Layer definitions, tile URLs, legends, available dates
    admin_boundaries.geojson  # Ward boundaries with pcodes
  patches/                  #   Patches applied to Prism source (e.g. date format support)

infra/                      # AWS infrastructure
  cloudformation/           #   12 nested CloudFormation templates
    main.yaml               #     Root stack orchestrator
    fargate-ingest.yaml     #     Ingest task (1 vCPU / 4 GB)
    fargate-serve.yaml      #     Serve task + ALB (2 vCPU / 8 GB)
    fargate-infer.yaml      #     Feature-extract + infer tasks (conditional)
    step-functions.yaml     #     Inference pipeline + Lambda trigger (conditional)
    cloudfront.yaml         #     HTTPS distribution with CORS
    amplify.yaml            #     Frontend hosting
    eventbridge.yaml        #     10-day ingest schedule
    s3.yaml, ecr.yaml, iam.yaml
  deploy-all.sh             #   One-command deployment

sagemaker-pipeline/         # ML training reference (not deployed)
  *.py, *.ipynb             #   Training scripts, notebooks, hyperparameter tuning
  config.yaml               #   Training configuration
  README.md                 #   Training pipeline documentation

prism-app/                  # WFP's Prism platform source (cloned at deploy time)
  frontend/                 #   React + MapLibre + Redux
  api/                      #   FastAPI backend for zonal stats
  alerting/                 #   Storm/flood/drought email alerting
  common/                   #   Shared TypeScript utilities

data/                       # Static data artifacts
  kenya_wards/              #   Shapefile source for ward boundaries

docs/                       # Documentation
  ARCHITECTURE.md           #   Full system architecture, S3 layout, API reference
  INGESTION_CONTAINER.md    #   How the backend container works
  COST_ESTIMATE.md          #   AWS cost breakdown (~$59-105/month)
  PLAN_INFERENCE_INTEGRATION.md   # Inference integration technical approach
  PROGRESS_INFERENCE_INTEGRATION.md  # Phase tracker
  archive/                  #   Historical planning docs
```

## Configuration

All platform configuration lives in [`backend/config/datasets.yaml`](backend/config/datasets.yaml):

**Datasets** — 10 enabled collections: 8 from Planetary Computer (NDVI, EVI, LAI, FPAR, LST day/night, surface reflectance, GPP) plus 2 from NASA Earthdata (8-day ET and PET). Disabled entries exist for Sentinel-1/2, fire, DEM, land cover, and the deprecated annual ET/PET. Each dataset specifies its data source (`planetary_computer` or `nasa_earthdata`), STAC collection, assets, resolution, and S3 key template.

**Admin levels** — Ward boundaries and filtering. Currently configured for 12 wards in Marsabit County. Adding a new county is a one-line YAML change (no code changes, no re-ingestion).

**Inference toggle** — `inference.enabled: true/false` controls whether Step Functions infrastructure is deployed. When disabled, only ingest + serve run.

```yaml
# Add a new county — just edit the filter:
admin_levels:
  - level: 3
    filter:
      field: "first_dist"
      values: ["Marsabit", "Turkana"]  # add here
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /collections` | List datasets with available dates |
| `GET /raster_geotiff?collection=&date=&asset=` | Presigned S3 URL for COG download |
| `GET /tile_url?collection=&date=&asset=` | TiTiler tile URL template |
| `GET /latest?model=livestock-mortality` | Latest prediction COG URL |
| `GET /predictions/livestock-mortality/dates` | Available prediction dates |
| `GET /predictions/livestock-mortality/{date}` | Ward prediction data (JSON) |
| `GET /cog/tiles/WebMercatorQuad/{z}/{x}/{y}` | TiTiler raster tiles |

## Infrastructure

Deployed via CloudFormation (`infra/cloudformation/main.yaml`). The deploy script auto-discovers the default VPC — no manual network setup needed.

| Resource | Name | Notes |
|----------|------|-------|
| ECS Cluster | `lmr-cluster-{env}` | Shared by all Fargate tasks |
| S3 Bucket | `lmr-data-cogs-{env}` | COGs, stats, models, predictions |
| ECR | `lmr-container-{env}` | Single container image |
| ALB | Internet-facing, port 80 | Health check on `/health` |
| CloudFront | HTTPS distribution | CORS, caching, HTTP→HTTPS redirect |
| Amplify | `lmr-prism-{env}` | Prism frontend hosting |
| EventBridge | Every 10 days | Triggers ingest task |
| Step Functions | `lmr-ward-inference-{env}` | When inference enabled |

Estimated cost: **$59-$105/month** depending on traffic. See [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md).

## Key Design Decisions

1. **Single container, multiple modes** — One Docker image for ingest, serve, feature-extract, and infer. Shared code stays in one place.
2. **Step Functions over SageMaker** — Inference via Step Functions + ECS Fargate instead of SageMaker Pipelines. Saves ~$1,000/year and avoids NAT gateway costs.
3. **Models loaded from S3** — Ensemble model bundles stored in the data bucket, not baked into Docker. Documented revert path to original SageMaker training bucket.
4. **Default VPC** — Auto-discovers the default VPC to avoid managed VPC + NAT gateway costs.
5. **COG format everywhere** — Cloud Optimized GeoTIFFs with tiled layout, overviews, and DEFLATE compression for efficient HTTP range reads.
6. **Config-driven extensibility** — New datasets, counties, or regions added by editing YAML. No code changes required.
7. **Prism cloned at deploy time** — Not checked into this repo. Pinned to a specific commit for reproducibility.
8. **Conditional inference** — `inference.enabled: false` deploys no inference infrastructure. WFP can use the platform for data ingestion and visualization without the ML pipeline.

## Docs

| Document | Description |
|----------|-------------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Full system architecture, container modes, S3 layout, API reference |
| [`docs/INGESTION_CONTAINER.md`](docs/INGESTION_CONTAINER.md) | How the backend container works, data flow, manual operation |
| [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md) | AWS cost breakdown and optimization options |
| [`docs/PLAN_INFERENCE_INTEGRATION.md`](docs/PLAN_INFERENCE_INTEGRATION.md) | Inference integration technical approach |
| [`docs/PROGRESS_INFERENCE_INTEGRATION.md`](docs/PROGRESS_INFERENCE_INTEGRATION.md) | Integration phase tracker |

## Team

UC Berkeley MIDS Capstone, Spring 2026. Client: World Food Programme.

| Name | Contact |
|------|---------|
| Abhas Wanchu | abhas@berkeley.edu |
| Conor Huh | conorhuh@berkeley.edu |
| Grace Murnaghan | grace.murnaghan@berkeley.edu |
| Skylar Wang | skylarmwang@berkeley.edu |
