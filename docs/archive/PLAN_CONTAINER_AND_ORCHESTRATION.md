# LMR Data Platform — Container & Orchestration Architecture Plan

## Overview

A single Docker container (two runtime modes) deployed on AWS Fargate, orchestrated via EventBridge and CloudFormation, that ingests satellite data from Microsoft Planetary Computer and serves COGs to the Prism frontend.

**Region:** `us-east-1`
**Naming convention:** `lmr-*`

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS Cloud (us-east-1)                      │
│                                                                     │
│  ┌──────────────┐    ┌─────────────────────┐    ┌───────────────┐  │
│  │ EventBridge   │───▶│ Fargate (ingest)    │───▶│ S3            │  │
│  │ (8-day cron)  │    │ lmr-container       │    │ lmr-data-*    │  │
│  └──────────────┘    └─────────────────────┘    └───────┬───────┘  │
│                                                         │          │
│                                              S3 Event   │          │
│                                              Notification          │
│                                                         ▼          │
│                                                ┌────────────────┐  │
│                                                │ SageMaker      │  │
│                                                │ Pipeline       │  │
│                                                │ (colleague)    │  │
│                                                └───────┬────────┘  │
│                                                        │           │
│                                                        ▼           │
│                                                ┌───────────────┐   │
│                                                │ S3             │   │
│                                                │ lmr-predictions│  │
│  ┌──────────┐    ┌─────┐    ┌──────────────┐   └───────┬───────┘   │
│  │ Amplify   │◀──│ ALB │◀──│ Fargate      │◀──────────┘           │
│  │ (Prism)   │   │     │   │ (serve mode) │                       │
│  └──────────┘    └─────┘    └──────────────┘                       │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────┐                          │
│  │ ECR           │    │ CloudFormation    │                         │
│  │ lmr-container │    │ lmr-stack         │                         │
│  └──────────────┘    └──────────────────┘                          │
└─────────────────────────────────────────────────────────────────────┘
```
****
---

## 2. Docker Container

### 2.1 Single Image, Two Modes

The container runs in one of two modes, selected via the `--mode` CLI flag:

```bash
# Ingest mode — triggered by EventBridge on schedule
docker run lmr-container --mode ingest --config /app/config/datasets.yaml

# Serve mode — long-running behind ALB
docker run lmr-container --mode serve --port 8000
```

### 2.2 Project Structure

```
lmr-container/
├── Dockerfile
├── pyproject.toml
├── config/
│   └── datasets.yaml          # STAC dataset configuration
├── src/
│   └── lmr/
│       ├── __init__.py
│       ├── cli.py             # Entrypoint, argparse (--mode, --config)
│       ├── config.py          # YAML config loader & validation
│       ├── ingest/
│       │   ├── __init__.py
│       │   ├── stac_client.py # STAC search & download logic
│       │   ├── cog.py         # COG processing (ensure COG format, clip to AOI)
│       │   └── s3.py          # S3 upload with predictable key structure
│       ├── serve/
│       │   ├── __init__.py
│       │   ├── app.py         # FastAPI application
│       │   ├── routes.py      # /raster_geotiff endpoint (Prism-compatible)
│       │   └── s3.py          # S3 presigned URL generation
│       └── common/
│           ├── __init__.py
│           ├── s3.py          # Shared S3 utilities
│           └── logging.py     # Structured logging
├── tests/
│   ├── test_config.py
│   ├── test_ingest.py
│   └── test_serve.py
└── cloudformation/
    ├── main.yaml              # Root stack
    ├── ecr.yaml               # ECR repository
    ├── fargate-ingest.yaml    # ECS task definition for ingest
    ├── fargate-serve.yaml     # ECS task definition for serve + ALB
    ├── eventbridge.yaml       # Scheduled rule
    ├── s3.yaml                # Buckets + event notifications
    └── iam.yaml               # Task execution roles & policies
```

### 2.3 Key Dependencies

```toml
[project]
name = "lmr-container"
requires-python = ">=3.11"

dependencies = [
    # STAC & geospatial
    "pystac-client>=0.7",
    "planetary-computer>=1.0",
    "rioxarray>=0.15",
    "rasterio>=1.3",
    "stackstac>=0.5",
    "xarray>=2024.0",
    "geopandas>=0.14",

    # AWS
    "boto3>=1.34",

    # Serve mode
    "fastapi>=0.110",
    "uvicorn>=0.29",

    # Config
    "pyyaml>=6.0",
    "pydantic>=2.0",
]
```

---

## 3. YAML Configuration (`datasets.yaml`)

This is the core configuration file. It defines what data to pull, where to store it, and how often to run.

```yaml
# datasets.yaml — LMR Data Platform Configuration

# Global settings
global:
  region: "us-east-1"
  schedule_interval_days: 8        # EventBridge periodicity (configurable)
  s3_bucket: "lmr-data-cogs"
  s3_prefix: "ingested"
  log_level: "INFO"

# Area of interest
aoi:
  name: "marsabit-county-kenya"
  # Bounding box [west, south, east, north]
  bbox: [36.0, 1.5, 42.0, 4.5]
  # Optional: GeoJSON file for precise boundary clipping
  # boundary_file: "boundaries/marsabit.geojson"

# STAC catalog configuration
stac:
  catalog_url: "https://planetarycomputer.microsoft.com/api/stac/v1"
  # Set to true if catalog requires planetary-computer token signing
  requires_signing: true

# Dataset definitions — add new datasets by appending to this list
datasets:
  - name: "ndvi-sentinel2"
    enabled: true
    collection: "sentinel-2-l2a"          # STAC collection ID
    assets: ["B04", "B08"]                # Asset keys to download
    query_filters:                         # Additional STAC query params
      eo:cloud_cover:
        lt: 30
    temporal:
      lookback_days: 16                   # How far back to search on each run
    processing:
      output_format: "cog"                # "cog" (future: "wms")
      resolution_m: 30                    # Target resolution in meters
      crs: "EPSG:4326"                    # Output CRS
    s3_key_template: "{prefix}/{dataset}/{date}/{asset}.tif"

  - name: "rainfall-chirps"
    enabled: true
    collection: "chirps-daily"
    assets: ["precip"]
    temporal:
      lookback_days: 16
    processing:
      output_format: "cog"
      resolution_m: 5000
      crs: "EPSG:4326"
    s3_key_template: "{prefix}/{dataset}/{date}/{asset}.tif"

  # Example: adding a new dataset (teammate just adds a block here)
  # - name: "soil-moisture"
  #   enabled: false
  #   collection: "some-collection-id"
  #   assets: ["sm"]
  #   temporal:
  #     lookback_days: 16
  #   processing:
  #     output_format: "cog"
  #     resolution_m: 1000
  #     crs: "EPSG:4326"
  #   s3_key_template: "{prefix}/{dataset}/{date}/{asset}.tif"
```

### 3.1 Configuration Design Principles

- **Modular**: Each dataset is a self-contained block. Add/remove datasets by editing YAML.
- **Extensible**: New STAC providers can be added by changing `catalog_url` per dataset (future enhancement: per-dataset catalog override).
- **Configurable schedule**: `schedule_interval_days` drives EventBridge rule — no code change needed to adjust frequency.
- **Template-based S3 keys**: Predictable key structure so the serve container and Prism frontend know where to find data.

---

## 4. Ingest Mode — Detailed Flow

### Phase 1 Implementation (today's build target)

```
EventBridge Rule (rate: 8 days)
        │
        ▼
ECS RunTask (lmr-container --mode ingest)
        │
        ▼
┌─────────────────────────┐
│ 1. Load datasets.yaml   │
│ 2. For each enabled      │
│    dataset:              │
│    a. Connect to STAC    │
│       catalog            │
│    b. Search by AOI,     │
│       time range,        │
│       filters            │
│    c. Sign assets (PC)   │
│    d. Download raster    │
│       data               │
│    e. Clip to AOI bbox   │
│    f. Ensure COG format  │
│    g. Upload to S3 with  │
│       predictable key    │
│ 3. Write manifest.json   │
│    to S3 (list of new    │
│    files written)        │
│ 4. Exit (task stops)     │
└─────────────────────────┘
        │
        ▼
S3 Event Notification (on manifest.json PutObject)
        │
        ▼
SageMaker Pipeline (colleague's scope — see separate plan)
```

### 4.1 Incremental Ingestion

After the initial manual bulk upload, each scheduled run only pulls **new data** since the last run:

- On each run, check S3 for the latest ingested date per dataset (via manifest or S3 listing)
- Set STAC search `datetime` to `{last_ingested_date}/{now}`
- Only download and process new items
- This keeps data volumes well under 20GB per run

### 4.2 S3 Key Structure

```
lmr-data-cogs/
├── ingested/
│   ├── ndvi-sentinel2/
│   │   ├── 2026-03-01/
│   │   │   ├── B04.tif
│   │   │   └── B08.tif
│   │   └── 2026-03-09/
│   │       ├── B04.tif
│   │       └── B08.tif
│   └── rainfall-chirps/
│       └── 2026-03-05/
│           └── precip.tif
├── predictions/                    # Written by SageMaker pipeline
│   └── livestock-mortality/
│       └── 2026-03-09/
│           └── prediction.tif
└── manifests/
    ├── ingest-2026-03-01.json
    └── ingest-2026-03-09.json
```

### 4.3 Manifest File

Each ingest run writes a manifest so downstream systems know what's new:

```json
{
  "run_id": "ingest-2026-03-09T00:00:00Z",
  "timestamp": "2026-03-09T01:23:45Z",
  "datasets_processed": [
    {
      "name": "ndvi-sentinel2",
      "items_ingested": 2,
      "s3_keys": [
        "ingested/ndvi-sentinel2/2026-03-09/B04.tif",
        "ingested/ndvi-sentinel2/2026-03-09/B08.tif"
      ],
      "stac_items": ["S2A_MSIL2A_20260309..."]
    }
  ],
  "status": "success"
}
```

---

## 5. Serve Mode — Detailed Flow

### Phase 2 Implementation

```
Prism Frontend (Amplify)
        │
        │  GET /raster_geotiff?collection=ndvi-sentinel2&date=2026-03-09&bbox=...
        ▼
ALB ──▶ Fargate (lmr-container --mode serve --port 8000)
        │
        ▼
┌──────────────────────────────┐
│ FastAPI Application          │
│                              │
│ GET /raster_geotiff          │
│   1. Parse params: collection│
│      date, bbox              │
│   2. Map to S3 key using     │
│      same template from YAML │
│   3. Verify object exists    │
│   4. Generate presigned URL  │
│      (1hr expiry)            │
│   5. Return presigned URL    │
│                              │
│ GET /health                  │
│   → 200 OK (ALB healthcheck)│
│                              │
│ GET /collections             │
│   → List available datasets  │
│     and date ranges from S3  │
│                              │
│ GET /latest                  │
│   → Latest prediction COG    │
│     presigned URL            │
└──────────────────────────────┘
```

### 5.1 Prism Compatibility

The `/raster_geotiff` endpoint must accept the same query parameters that Prism's backend currently uses:

| Parameter    | Type   | Description                        |
|------------- |--------|------------------------------------|
| `collection` | string | Dataset name (maps to YAML config) |
| `date`       | string | ISO date for the data              |
| `bbox`       | string | Bounding box (west,south,east,north)|

**Response:** JSON with presigned S3 URL, matching Prism's expected response format.

### 5.2 Serving Predictions

The serve container reads from both:
- `ingested/` — raw satellite COGs from ingest pipeline
- `predictions/` — model output COGs from SageMaker pipeline

Both are served through the same `/raster_geotiff` interface. The `collection` parameter distinguishes between raw data and predictions (e.g., `collection=livestock-mortality-prediction`).

---

## 6. CloudFormation Templates

### 6.1 Stack Structure

A root stack (`main.yaml`) that references nested stacks:

| Template               | Resources                                               |
|------------------------|---------------------------------------------------------|
| `main.yaml`           | Root stack, parameters, nested stack references          |
| `ecr.yaml`            | `lmr-container` ECR repository                          |
| `s3.yaml`             | `lmr-data-cogs` bucket, event notification config       |
| `iam.yaml`            | ECS task execution role, task role (S3, ECR, logs)       |
| `fargate-ingest.yaml` | ECS cluster, task definition (ingest mode), log group    |
| `fargate-serve.yaml`  | ECS service, task definition (serve mode), ALB, TG       |
| `eventbridge.yaml`    | Scheduled rule → ECS RunTask target                      |

### 6.2 Key Parameters

```yaml
Parameters:
  Environment:
    Type: String
    Default: "dev"
    AllowedValues: ["dev", "staging", "prod"]
  ScheduleIntervalDays:
    Type: Number
    Default: 8
  ContainerImageTag:
    Type: String
    Default: "latest"
  VpcId:
    Type: AWS::EC2::VPC::Id
    Description: "Existing VPC to deploy into"
  SubnetIds:
    Type: List<AWS::EC2::Subnet::Id>
    Description: "Existing subnets for Fargate tasks and ALB"
```

### 6.3 IAM Permissions (Task Role)

The Fargate task role needs:
- `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on `lmr-data-cogs`
- `ecr:GetAuthorizationToken`, `ecr:BatchGetImage` (managed by execution role)
- `logs:CreateLogStream`, `logs:PutLogEvents`
- `ssm:GetParameter` (for future config reads)

---

## 7. Implementation Phases

### Phase 1: Ingest Pipeline (Target: today)

| Step | Task                                               | Details                              |
|------|-----------------------------------------------------|--------------------------------------|
| 1    | Create project scaffolding                          | Directory structure, pyproject.toml  |
| 2    | Write `datasets.yaml` with placeholder collections  | Full schema, commented examples      |
| 3    | Implement config loader (`config.py`)               | Pydantic models, YAML parsing        |
| 4    | Implement STAC client (`stac_client.py`)            | Search, sign, download               |
| 5    | Implement COG processor (`cog.py`)                  | Clip to AOI, ensure COG format       |
| 6    | Implement S3 uploader (`s3.py`)                     | Key templating, manifest writing     |
| 7    | Implement CLI entrypoint (`cli.py`)                 | `--mode ingest` flow                 |
| 8    | Write Dockerfile                                    | Multi-stage build, GDAL deps         |
| 9    | Write CloudFormation templates                      | ECR, S3, IAM, Fargate ingest, EB     |
| 10   | Local testing with sample STAC query                | Verify end-to-end locally            |

### Phase 2: Serve API (next session)

| Step | Task                                               | Details                              |
|------|-----------------------------------------------------|--------------------------------------|
| 1    | Implement FastAPI app (`app.py`, `routes.py`)       | /raster_geotiff, /health, /collections |
| 2    | Implement S3 presigned URL generation               | Lookup + presign                     |
| 3    | Add serve mode to CLI                               | `--mode serve --port 8000`           |
| 4    | Write CloudFormation for serve Fargate + ALB        | Service, target group, listener      |
| 5    | Integration test with Prism frontend                | Point Amplify at ALB                 |

### Phase 3: Future Enhancements (not in scope)

- Map tile generation (TiTiler integration) for WMS compatibility
- Per-dataset STAC catalog URL overrides
- GeoJSON boundary clipping (beyond bbox)
- Multi-region deployment

---

## 8. Deployment Workflow

```bash
# 1. Build and push container
docker build -t lmr-container .
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag lmr-container:latest <account>.dkr.ecr.us-east-1.amazonaws.com/lmr-container:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/lmr-container:latest

# 2. Deploy CloudFormation
aws cloudformation deploy \
  --template-file cloudformation/main.yaml \
  --stack-name lmr-platform-dev \
  --parameter-overrides Environment=dev ScheduleIntervalDays=8 \
  --capabilities CAPABILITY_IAM \
  --region us-east-1

# 3. Manual first ingest (one-time bulk load)
aws ecs run-task \
  --cluster lmr-cluster \
  --task-definition lmr-ingest \
  --launch-type FARGATE \
  --network-configuration "..." \
  --overrides '{"containerOverrides":[{"name":"lmr-container","command":["--mode","ingest","--config","/app/config/datasets.yaml","--full-history"]}]}'
```

---

## 9. Open Items / Decisions Needed

| Item | Owner | Notes |
|------|-------|-------|
| Exact STAC collection IDs and asset keys | Teammate | Populate in `datasets.yaml` |
| Marsabit County precise bbox coordinates | Teammate / Conor | Currently placeholder |
| VPC and subnet IDs for Fargate | Conor | Pass existing VPC/subnet IDs as CF parameters |
| Prism `/raster_geotiff` response format | Conor | Need exact JSON shape from forked Prism repo |
| Initial bulk data upload approach | Conor | Manual notebook run → S3 upload |
| S3 bucket for SageMaker pipeline outputs | Colleague | Same bucket (`predictions/` prefix) or separate? |
