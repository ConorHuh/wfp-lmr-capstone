# WFP Livestock Mortality Risk (LMR) Platform

Early warning system for livestock mortality in Kenya's Marsabit County. Ingests satellite imagery (MODIS), serves raster tiles via TiTiler, and visualizes predictions through WFP's Prism frontend.

## Quick Start — One-Command Deploy

```bash
./scripts/deploy-all.sh
```

This single script deploys the entire platform:
1. **Backend** — Builds Docker image, deploys CloudFormation (ECR, S3, ECS, IAM, EventBridge), pushes to ECR, updates Fargate services
2. **CloudFront** — Configures CORS (Origin header forwarding + OPTIONS preflight)
3. **Frontend** — Clones Prism at a pinned commit, injects Kenya config, builds, deploys to Amplify

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS CLI | v2 | `brew install awscli` |
| Docker | Running | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| Node.js | 20 (via nvm) | `nvm install 20` |
| yarn | latest | `npm install -g yarn` |
| nvm | any | `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh \| bash` |

AWS credentials must be configured (`aws sts get-caller-identity` should succeed).

### Selective Deployment

```bash
./scripts/deploy-all.sh --skip-backend      # frontend + CloudFront only
./scripts/deploy-all.sh --skip-frontend     # backend + CloudFront only
./scripts/deploy-all.sh --skip-build        # skip Docker build (use existing ECR image)
./scripts/deploy-all.sh --skip-cloudfront   # skip CloudFront CORS check
```

Run `./scripts/deploy-all.sh --help` for all options.

## Repo Structure

```
├── lmr-container/              # The core container (ingest + serve modes)
│   ├── src/lmr/                # Python source
│   │   ├── ingest/             # Satellite data ingestion pipeline
│   │   ├── serve/              # TiTiler tile server + API
│   │   └── common/             # Shared utilities (S3, config, logging)
│   ├── cloudformation/         # AWS infrastructure (nested stacks)
│   │   ├── main.yaml           # Root stack
│   │   ├── fargate-ingest.yaml # Ingest ECS task
│   │   ├── fargate-serve.yaml  # Serve ECS service + ALB
│   │   ├── iam.yaml            # Task roles + policies
│   │   ├── s3.yaml             # Data bucket
│   │   ├── ecr.yaml            # Container registry
│   │   ├── eventbridge.yaml    # Ingest schedule (every N days)
│   │   └── sagemaker-trigger.yaml  # (optional) manifest → SageMaker pipeline
│   ├── config/datasets.yaml    # Dataset definitions
│   ├── Dockerfile
│   └── pyproject.toml
│
├── prism/                      # Prism frontend configuration (injected at build time)
│   ├── kenya_config/
│   │   ├── layers.json         # Layer definitions (tile URLs, legends, dates)
│   │   ├── prism.json          # App config (map center, categories, icons)
│   │   └── admin_boundaries.geojson  # Ward boundaries (ADM3)
│   └── patches/                # Patches applied to prism-app during build
│       └── 0001-support-hyphenated-date-format-in-static-raster-urls.patch
│
├── scripts/
│   ├── deploy-all.sh           # ← THE deploy script
│   └── truncate_precision.sh   # Utility: reduce GeoJSON coordinate precision
│
└── docs/                       # Architecture, cost estimates, conversion guides
```

## Architecture

```
                    ┌──────────────────────────────┐
                    │  Amplify (Prism Frontend)     │
                    │  main.d3dvy50qlv6dr6.         │
                    │       amplifyapp.com          │
                    └──────────────┬───────────────┘
                                   │ tile requests
                                   ▼
                    ┌──────────────────────────────┐
                    │  CloudFront (HTTPS + CORS)    │
                    │  d31fsorf4vwo9f.cloudfront.net│
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │  ALB → Fargate (Serve)        │
                    │  FastAPI + TiTiler            │
                    │  Reads COGs via /vsis3/       │
                    └──────────────┬───────────────┘
                                   │ range reads
                                   ▼
                    ┌──────────────────────────────┐
                    │  S3: lmr-data-cogs-dev        │
                    │  ├── ingested/modis-ndvi/...  │
                    │  └── predictions/...          │
                    └──────────────────────────────┘
                                   ▲
                                   │ writes COGs
                    ┌──────────────────────────────┐
                    │  Fargate (Ingest)             │
                    │  Planetary Computer → COG → S3│
                    │  Triggered every 10 days      │
                    └──────────────────────────────┘
```

## Key Configuration

All infrastructure IDs are at the top of `scripts/deploy-all.sh`:

| Variable | Current Value | Description |
|----------|---------------|-------------|
| `VPC_ID` | `vpc-0c392a79120ac5b1c` | VPC for Fargate tasks |
| `SUBNET_IDS` | `subnet-084b...,subnet-0f79...` | Subnets for Fargate + ALB |
| `AMPLIFY_APP_ID` | `d3dvy50qlv6dr6` | Amplify app (manual deploy) |
| `CLOUDFRONT_DISTRIBUTION_ID` | `E1GZRKL82M95B5` | HTTPS proxy for tile server |
| `PRISM_COMMIT` | `6f22f3b...` | Pinned prism-app commit |
| `S3_BUCKET` | `lmr-data-cogs-dev` | COG storage bucket |

## Adding New Data

### New satellite layer
1. Add ingestion logic in `lmr-container/src/lmr/ingest/`
2. Add layer definition in `prism/kenya_config/layers.json`
3. Add to a category in `prism/kenya_config/prism.json`
4. Deploy: `./scripts/deploy-all.sh`

### New prediction dates
1. Upload COG to `s3://lmr-data-cogs-dev/predictions/livestock-mortality/{YYYY-MM-DD}/prediction.tif`
2. Add the date to the `dates` array in `prism/kenya_config/layers.json`
3. Deploy frontend: `./scripts/deploy-all.sh --skip-backend`

See `docs/PREDICTION_COG_CONVERSION.md` for how to convert prediction GeoTIFFs to COG format.

## Development

```bash
cd lmr-container
uv sync                                    # install dependencies
uv run pytest -v                           # run tests
uv run lmr --mode serve --config config/datasets.yaml  # local serve
uv run lmr --mode ingest --config config/datasets.yaml # local ingest
```

## Documentation

- `docs/ARCHITECTURE.md` — Detailed architecture and data flow
- `docs/COST_ESTIMATE.md` — AWS cost breakdown
- `docs/INGESTION_CONTAINER.md` — How the ingest pipeline works
- `docs/PREDICTION_COG_CONVERSION.md` — GeoTIFF → COG conversion process
- `docs/PLAN_DECOUPLE_SAGEMAKER.md` — Future: decouple from SageMaker
