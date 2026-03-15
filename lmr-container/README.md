# LMR Container

Single Docker container for the WFP Livestock Mortality Risk data platform. Two runtime modes: **ingest** (pulls satellite data) and **serve** (API for Prism frontend).

## Architecture

```
EventBridge (8-day schedule)
    │
    ▼
Fargate (--mode ingest)
    │
    ├─▶ STAC Search (Planetary Computer) ─▶ Download ─▶ Clip (Kenya bbox) ─▶ COG ─▶ S3
    │
    └─▶ Zonal Stats (per ward) ─▶ S3
    │
    ▼
S3 Event ─▶ SageMaker Pipeline (colleague) ─▶ Prediction COGs ─▶ S3
    │
    ▼
Fargate (--mode serve) ─▶ ALB ─▶ Prism Frontend
```

## Quick Start

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run CLI
uv run lmr --mode ingest --config config/datasets.yaml
uv run lmr --mode serve --config config/datasets.yaml --port 8000
```

## Project Structure

```
lmr-container/
├── config/
│   ├── datasets.yaml              # Main configuration file
│   └── boundaries/
│       └── kenya_wards.geojson    # Admin level 3 ward boundaries (1,425 wards)
├── src/lmr/
│   ├── cli.py                     # Entrypoint (--mode ingest|serve)
│   ├── config.py                  # Pydantic config models + YAML loader
│   ├── ingest/
│   │   ├── stac_client.py         # STAC catalog search (Planetary Computer)
│   │   ├── cog.py                 # Download, clip, reproject to COG
│   │   ├── s3.py                  # S3 upload, key templating, manifests
│   │   └── zonal.py               # Per-ward zonal statistics
│   ├── serve/
│   │   ├── app.py                 # FastAPI app factory
│   │   └── routes.py              # API endpoints (/health, Phase 2: /raster_geotiff, /stats)
│   └── common/
│       ├── s3.py                  # Shared S3 client
│       └── logging.py             # Structured JSON logging
├── cloudformation/                # AWS infrastructure (6 nested stacks)
├── tests/
├── Dockerfile
└── pyproject.toml
```

## Configuration

All configuration lives in `config/datasets.yaml`. Key sections:

### AOI (Area of Interest)

Ingestion pulls data for all of Kenya. The bbox covers the full country extent.

```yaml
aoi:
  name: "kenya"
  bbox: [33.91, -4.80, 41.91, 5.41]
```

### Admin Levels

Controls which regions get zonal statistics. Ingestion scope is always country-wide — admin levels only filter the stats computation. Adding new counties is a YAML change, no re-ingestion needed.

```yaml
admin_levels:
  - level: 3
    name: "wards"
    boundary_file: "boundaries/kenya_wards.geojson"
    id_field: "pcode"          # unique ward ID (e.g., KE0842)
    name_field: "iebc_wards"   # human-readable name
    filter:
      field: "first_dist"
      values: ["Marsabit"]    # add more districts here
```

Current Marsabit wards (12): Dukana, Illeret, Karare, Kargi/South Horr, Korr/Ngurunit, Logologo, Loiyangalani, Maikona, Marsabet Central, North Horr, Sagante/Jaldessa, Turbi.

### Datasets

Each dataset is a self-contained block. The `collection` and `assets` fields map to STAC catalog entries.

```yaml
datasets:
  - name: "ndvi-sentinel2"
    collection: "sentinel-2-l2a"
    assets: ["B04", "B08"]
    # ...
```

## Ingest Flow

1. Load `datasets.yaml` config and ward boundaries
2. For each enabled dataset:
   - Query STAC catalog (Planetary Computer) by Kenya bbox and time range
   - Download raster assets
   - Clip to Kenya bbox
   - Reproject and write as Cloud Optimized GeoTIFF (COG)
   - Upload COG to S3: `ingested/{dataset}/{date}/{asset}.tif`
   - Compute zonal stats per filtered ward
   - Upload stats to S3: `stats/{dataset}/{date}/admin3_{asset}.json`
3. Write manifest JSON to `manifests/ingest-{timestamp}.json`
4. S3 event triggers downstream SageMaker pipeline

### Incremental Ingestion

After the initial run, each scheduled run checks S3 for the latest ingested date per dataset and only pulls new data since then. Use `--full-history` to override.

## S3 Key Structure

```
lmr-data-cogs-{env}/
├── ingested/                          # Country-wide satellite COGs
│   ├── ndvi-sentinel2/
│   │   └── 2026-03-09/
│   │       ├── B04.tif
│   │       └── B08.tif
│   └── rainfall-chirps/
│       └── 2026-03-05/
│           └── precip.tif
├── stats/                             # Per-ward zonal statistics
│   ├── ndvi-sentinel2/
│   │   └── 2026-03-09/
│   │       └── admin3_B04.json        # {id, name, admin_level, stats: {mean, median, ...}}
│   └── rainfall-chirps/
│       └── 2026-03-05/
│           └── admin3_precip.json
├── predictions/                       # ML model outputs (SageMaker)
│   └── livestock-mortality/
│       └── 2026-03-09/
│           └── prediction.tif
└── manifests/
    └── ingest-2026-03-09T00:00:00Z.json
```

## CloudFormation

Six nested stacks deployed via `main.yaml`:

| Stack | Resources |
|-------|-----------|
| `ecr.yaml` | ECR repository (`lmr-container-{env}`) |
| `s3.yaml` | S3 bucket with EventBridge notifications, versioning, IA lifecycle |
| `iam.yaml` | Execution role, task role (S3/SSM/logs), EventBridge role |
| `fargate-ingest.yaml` | ECS cluster, task definition (1 vCPU, 4GB), security group, log group |
| `eventbridge.yaml` | Scheduled rule (every N days) targeting ECS RunTask |
| `main.yaml` | Root stack wiring parameters and outputs |

### Deploy

Use the deploy script in `scripts/deploy.sh`. It handles Docker build, ECR push, CloudFormation packaging, and stack deployment.

**Prerequisites:**
- AWS CLI configured with appropriate credentials
- Docker running
- An S3 bucket for CloudFormation artifacts: `lmr-cfn-artifacts-{env}` (create once per environment)

```bash
# First-time setup: create the CFN artifacts bucket
aws s3 mb s3://lmr-cfn-artifacts-dev --region us-east-1

# Full deploy (build + push + deploy stack)
./scripts/deploy.sh --env dev --vpc-id vpc-xxx --subnet-ids subnet-aaa,subnet-bbb

# Deploy to staging with a specific tag
./scripts/deploy.sh --env staging --tag v1.0.0 --vpc-id vpc-xxx --subnet-ids subnet-aaa,subnet-bbb

# Redeploy stack only (skip Docker build)
./scripts/deploy.sh --env dev --skip-build

# Custom schedule (e.g., every 10 days)
./scripts/deploy.sh --env dev --schedule-days 10
```

**All deploy options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--env` | Environment (dev/staging/prod) | `dev` |
| `--tag` | Docker image tag | `latest` |
| `--region` | AWS region | `us-east-1` |
| `--vpc-id` | VPC ID (required on first deploy) | vpc-0c392a79120ac5b1c |
| `--subnet-ids` | Comma-separated subnet IDs (required on first deploy) | subnet-0dad0b63d1d403190 |
| `--schedule-days` | Ingest schedule interval in days | `8` |
| `--skip-build` | Skip Docker build and push | `false` |
| `--stack-name` | Override stack name | `lmr-platform-{env}` |

**Manual first ingest** (after deploy):

```bash
aws ecs run-task \
  --cluster lmr-cluster-dev \
  --task-definition lmr-ingest-dev \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaa],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"lmr-container","command":["--mode","ingest","--config","/app/config/datasets.yaml","--full-history"]}]}' \
  --region us-east-1
```

## Development

```bash
uv sync                          # install all deps
uv run pytest -v                 # run tests
uv run ruff check src/ tests/    # lint
uv add <package>                 # add a dependency
```
