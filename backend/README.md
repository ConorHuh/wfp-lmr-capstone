# LMR Backend Container

Single Docker container for the WFP Livestock Mortality Risk data platform. Four runtime modes: **ingest** (pulls satellite data), **serve** (API for Prism frontend), **feature-extract** (ward-level feature engineering), and **infer** (ensemble ML predictions).

## Quick Start

```bash
uv sync --group dev              # install dependencies
uv run pytest tests/ -v          # run tests (54 tests)
uv run lmr --mode serve --config config/datasets.yaml     # tile server
uv run lmr --mode ingest --config config/datasets.yaml    # satellite ingestion
```

## Container Modes

| Mode | Command | Fargate Resources | Purpose |
|------|---------|-------------------|---------|
| `ingest` | `lmr --mode ingest` | 1 vCPU / 4 GB | Pull MODIS data from Planetary Computer, convert to COGs, upload to S3 |
| `serve` | `lmr --mode serve` | 2 vCPU / 8 GB | FastAPI + TiTiler tile server behind ALB + CloudFront |
| `feature-extract` | `lmr --mode feature-extract --time-start YYYY-MM --time-end YYYY-MM` | 4 vCPU / 16 GB | Ward-level satellite feature extraction |
| `infer` | `lmr --mode infer --scheme biannual\|quadseasonal\|monthly` | 1 vCPU / 4 GB | Ensemble inference for one temporal scheme |

## Project Structure

```
backend/
├── config/
│   ├── datasets.yaml              # All platform configuration
│   └── boundaries/
│       └── kenya_wards.geojson    # Admin level 3 ward boundaries (1,425 wards)
├── src/lmr/
│   ├── cli.py                     # Entrypoint (--mode ingest|serve|infer|feature-extract)
│   ├── config.py                  # Pydantic config models + YAML loader
│   ├── ingest/
│   │   ├── stac_client.py         # STAC catalog search (Planetary Computer)
│   │   ├── cog.py                 # Download, clip, reproject to COG
│   │   ├── s3.py                  # S3 upload, key templating, manifests
│   │   └── zonal.py               # Per-ward zonal statistics
│   ├── serve/
│   │   ├── app.py                 # FastAPI app factory + TiTiler mount
│   │   ├── routes.py              # API endpoints (health, collections, predictions, tiles)
│   │   ├── s3.py                  # Presigned URLs, S3 helpers
│   │   └── titiler_setup.py       # TiTiler COG tiler factory
│   ├── infer/
│   │   ├── feature_extract.py     # Feature extraction entry point
│   │   ├── ward_features.py       # Ward-level feature engineering pipeline
│   │   ├── pipeline.py            # Orchestrates preprocess → ensemble → postprocess
│   │   ├── preprocess.py          # Impute NaNs, scale features
│   │   ├── ensemble.py            # 4-model ensemble + monthly meta-learner
│   │   └── postprocess.py         # Risk levels, GeoJSON/GeoTIFF/CSV output
│   └── common/
│       ├── s3.py                  # Shared S3 client
│       └── logging.py             # Structured JSON logging
├── tests/                         # 54 pytest tests
├── Dockerfile                     # Python 3.11-slim + GDAL, built with uv
└── pyproject.toml                 # Dependencies managed by uv
```

## Configuration

All configuration is in `config/datasets.yaml`. See the root [README](../README.md) for details on the inference toggle and adding new counties.

## Docker Build

```bash
docker build --platform linux/amd64 -t lmr-container:latest .
```

Always use `--platform linux/amd64` for Fargate, even when building on Apple Silicon.

## Deployment

Use `infra/deploy-all.sh` from the repo root — it handles Docker build, ECR push, CloudFormation, and ECS updates. See the root [README](../README.md) for deployment instructions.
