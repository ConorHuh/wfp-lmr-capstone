# LMR Data Platform — Architecture and Data Flow Guide

## Overview

The Livestock Mortality Risk (LMR) Data Platform is a system that ingests satellite imagery, processes it into analysis-ready formats, serves it as map tiles, and (in the planned final state) runs a machine learning model to predict livestock mortality risk across Kenya's administrative wards. It is built for the World Food Programme and designed to run entirely on AWS with minimal operational cost.

The platform has three major subsystems: an ingestion pipeline that pulls satellite data from Microsoft's Planetary Computer and stores it in S3, a tile-serving API that makes that data viewable on a map, and a web frontend that lets users explore the data interactively. A fourth subsystem — inference — is planned but not yet connected to a live model.

---

## System Components

### The Container

The entire backend lives in a single Docker container image. The same image runs in three different modes depending on the command it receives at launch:

- **Ingest mode** searches for new satellite imagery, downloads it, processes it into Cloud Optimized GeoTIFFs (COGs), and uploads the results to S3. It runs on a schedule — every 8 days — launched as a one-shot Fargate task by EventBridge.

- **Serve mode** starts a FastAPI web server that reads COGs from S3 and serves them as map tiles. It also provides API endpoints for listing available datasets, generating download links, and returning tile URL templates for the frontend. It runs continuously as a Fargate service behind a load balancer.

- **Infer mode** (planned) loads a trained machine learning model from S3, runs predictions against the latest satellite features, rasterizes the ward-level results into a COG, and uploads the prediction to S3.

The container is built from `python:3.11-slim` with GDAL system libraries for geospatial processing. Dependencies are managed with `uv` and defined in `pyproject.toml`. The entrypoint is a CLI tool (`lmr`) that accepts `--mode ingest|serve|infer` along with a path to the configuration file.

### Configuration

All behavior is driven by a single YAML configuration file (`config/datasets.yaml`). This file defines:

- **Global settings**: the AWS region, S3 bucket name, object key prefix, logging level, and the schedule interval.
- **Area of interest**: the bounding box for Kenya (`[33.91, -4.80, 41.91, 5.41]`), which controls what geographic extent gets clipped from the raw satellite data.
- **Admin boundaries**: the ward-level GeoJSON file used for zonal statistics, including which field identifies each ward and an optional filter (currently set to Marsabit County).
- **STAC catalog**: the Planetary Computer API URL and whether items need to be cryptographically signed before download.
- **Dataset definitions**: one entry per satellite product, specifying the STAC collection name, which asset bands to download, how far back to search, what resolution and CRS to reproject to, and the S3 key template for where to store the output.
- **Serve settings**: CORS origins, presigned URL expiration, and the S3 prefix where predictions are stored.
- **Inference settings**: the model name, where to find model artifacts in S3, and the output path for prediction COGs.

The configuration is validated at startup by Pydantic models in `config.py`. If any required field is missing or has an invalid value, the container fails fast with a clear error.

### S3 Bucket

A single S3 bucket (`lmr-data-cogs-dev`) is the central data store. Everything the platform produces and consumes lives here, organized by prefix:

- `ingested/{dataset-name}/{YYYY-MM-DD}/{asset}.tif` — processed COG files from the ingest pipeline. One file per asset per date. For datasets with multiple bands (like surface reflectance with 4 bands), each band gets its own file within the same date folder.
- `stats/{dataset-name}/{YYYY-MM-DD}/admin3_{asset}.json` — zonal statistics computed during ingest. One JSON file per asset per date, containing mean, median, min, max, and standard deviation for each ward in Marsabit County.
- `manifests/ingest-{timestamp}.json` — a record of each ingest run, listing which datasets were processed, how many items were ingested, and which S3 keys were written.
- `models/{model-name}/latest/` — (planned) trained model artifacts loaded by the inference mode.
- `predictions/{model-name}/{YYYY-MM-DD}/prediction.tif` — (planned) prediction COGs written by the inference mode.

The bucket has versioning enabled and a lifecycle rule that transitions objects to S3 Standard-IA after 90 days. EventBridge notifications are enabled on the bucket so that new manifest uploads can trigger downstream workflows.

### Planetary Computer (STAC Catalog)

The ingestion pipeline discovers satellite imagery through the STAC (SpatioTemporal Asset Catalog) API hosted by Microsoft's Planetary Computer. The platform searches for items matching a specific STAC collection, geographic bounding box, and time range. Items returned from the search are STAC metadata records that include signed URLs pointing to the actual raster data stored in Azure Blob Storage.

The Planetary Computer requires that items be cryptographically signed before their asset URLs can be accessed. These signatures (SAS tokens) expire after a period of time. The platform handles this by re-signing each item immediately before downloading its assets, and retrying with a fresh signature if a download fails with an HTTP 403.

### ECS Cluster and Fargate

Both the ingest and serve tasks run on the same ECS cluster (`lmr-cluster-dev`) using AWS Fargate — serverless containers that require no EC2 instance management.

The **ingest task** is configured with 1 vCPU and 4 GB of memory. It is not a persistent service — EventBridge launches it as a standalone task every 8 days. It runs to completion (processing all enabled datasets), writes a manifest, and exits. It runs on standard Fargate pricing because interruptions during a multi-hour ingest would waste significant work.

The **serve task** is configured with 2 vCPU and 8 GB of memory. It runs as a persistent ECS service with a desired count of 1. It uses Fargate Spot (70% discount) because the workload is stateless — if AWS reclaims the capacity, ECS automatically relaunches the task within seconds and the only impact is a brief interruption in tile serving. The task runs in dedicated public subnets (`subnet-084b074095d2d5a57` in us-east-1a and `subnet-0f79437f160c57ed3` in us-east-1f) that were created specifically for the ALB and serve container, separate from the subnets used by other services like SageMaker.

The serve container includes GDAL environment variables that optimize S3 COG reads: disabling directory listings on open, enabling VSI caching, and enabling HTTP multiplexing and consecutive range merging. These are critical for TiTiler performance because every tile request involves partial HTTP range reads against S3 objects.

### Application Load Balancer

An internet-facing ALB (`lmr-serve-dev`) sits in front of the serve Fargate task. It listens on port 80 (HTTP) and forwards traffic to port 8000 on the container. The target group uses the `/health` endpoint for health checks, polling every 30 seconds. A target must pass 2 consecutive health checks to be marked healthy and fail 3 to be marked unhealthy.

The ALB has its own security group that allows inbound TCP on port 80 from anywhere. The Fargate task's security group only allows inbound TCP on port 8000 from the ALB's security group — no direct internet access to the container.

### CloudFront

A CloudFront distribution (`d31fsorf4vwo9f.cloudfront.net`) sits in front of the ALB to provide HTTPS. This exists because the Prism frontend is hosted on Amplify (which serves over HTTPS), and browsers block mixed content — HTTP tile requests from an HTTPS page are silently dropped. CloudFront terminates TLS, caches tile responses, and forwards requests to the ALB over HTTP.

CloudFront is configured with query string forwarding enabled (required because tile URLs include the S3 COG path, rescale range, and colormap as query parameters). The default TTL is 24 hours, which means tiles are cached at CloudFront edge locations and subsequent requests for the same tile do not hit the ALB.

### Amplify (Frontend Hosting)

The Prism web application is deployed as a static site on AWS Amplify (`main.d3dvy50qlv6dr6.amplifyapp.com`). The site is password-protected. Amplify serves the built frontend assets over HTTPS with no server-side rendering — it is purely a static deployment of the compiled React application.

### EventBridge

An EventBridge rule triggers the ingest Fargate task on a schedule (every 8 days). The rule targets the ECS RunTask API with the ingest task definition, cluster, and network configuration. An IAM role grants EventBridge permission to run ECS tasks and pass the necessary execution and task roles.

### IAM Roles

Three IAM roles govern the platform's permissions:

The **Execution Role** is used by ECS to pull container images from ECR and write logs to CloudWatch. It uses the AWS-managed `AmazonECSTaskExecutionRolePolicy`.

The **Task Role** is assumed by the running container. It grants read/write access to the data S3 bucket, permission to write CloudWatch log streams, and permission to read SSM parameters under the `/lmr/` prefix. This single role is shared by both the ingest and serve tasks.

The **EventBridge Role** is assumed by the EventBridge service to launch ECS tasks. It has `ecs:RunTask` permission and `iam:PassRole` for the execution and task roles.

---

## The Serve API

The serve container runs a FastAPI application with two router groups: custom routes for platform-specific operations and a TiTiler router for COG tile serving.

### Custom Routes

**`GET /health`** returns `{"status": "ok"}`. Used by the ALB target group for health checks.

**`GET /collections`** lists all enabled datasets from the configuration along with their available dates in S3. For each dataset, it queries S3 to find date folders under `ingested/{dataset-name}/` and returns them sorted. This endpoint is useful for discovering what data is available without hardcoding dates in the frontend.

**`GET /raster_geotiff`** accepts a collection name, date, and asset key, resolves the S3 key, checks that the object exists, and returns a presigned URL that allows direct download of the COG for one hour.

**`GET /tile_url`** accepts the same parameters and returns a TiTiler tile URL template that the frontend can use to request map tiles. The template includes the S3 path as a query parameter.

**`GET /latest`** finds the most recent prediction COG for a given model name by listing date folders under `predictions/{model-name}/` and returning a presigned URL for the newest one.

### TiTiler Router

TiTiler is mounted at the `/cog` prefix. It provides standardized endpoints for serving tiles from Cloud Optimized GeoTIFFs. The key endpoint is `/cog/tiles/WebMercatorQuad/{z}/{x}/{y}` which accepts a `url` query parameter pointing to a COG in S3, reads just the bytes needed for that tile using HTTP range requests via GDAL's `/vsis3/` virtual filesystem, applies rescaling and a colormap, and returns a PNG tile.

TiTiler reads S3 using the Fargate task role's IAM credentials — no presigned URLs are needed for tile serving. The GDAL environment variables configured in the Dockerfile and task definition optimize this path by caching partial reads and multiplexing HTTP connections.

When a tile request falls outside the bounds of the COG (e.g., the user pans to an area not covered by the data), the application returns a pre-computed transparent 256x256 PNG instead of an error. This prevents the map frontend from displaying decode errors for out-of-bounds tiles.

### CORS

The serve application enables CORS middleware with configurable origins (defaulting to `*` to allow all origins). This is required because the Prism frontend on Amplify makes cross-origin requests to the tile server. Only GET methods are allowed.

---

## The Prism Frontend

Prism is WFP's open-source crisis mapping platform. The LMR deployment uses a Kenya-specific configuration that defines what layers are available and how they connect to the tile server.

### Configuration Structure

The frontend loads two JSON configuration files at build time:

**`prism.json`** defines the map center (latitude 2.87, longitude 37.54, zoom 7.5 — centered on Marsabit County), which boundary layer to display by default, and the category hierarchy for organizing layers in the sidebar. Layers are grouped under "hazards" with subcategories for vegetation indices, climate, and surface reflectance.

**`layers.json`** defines each individual layer. There are two layer types:

The **boundary layer** (`admin_boundaries`) is a vector GeoJSON layer showing Kenya's ADM3 wards. It references a local GeoJSON file bundled with the frontend, styled with transparent fill and gray outlines. The admin hierarchy has three levels: province, district, and ward. Ward boundaries are identified by their `pcode` field.

The **raster layers** (all MODIS products) are `static_raster` type layers served as XYZ tile pyramids. Each layer definition includes a title, opacity, zoom range, a base URL template, an array of available dates, legend text, and a color legend.

### Tile URL Pattern

Every raster layer's `base_url` follows this structure:

```
https://{cloudfront-domain}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}
  ?url=s3://{bucket}/ingested/{dataset}/{YYYY-MM-DD}/{asset}.tif
  &rescale={min},{max}
  &colormap_name={colormap}
```

When the user selects a date from the temporal slider, the frontend substitutes `{YYYY-MM-DD}` with the selected date string and requests tiles for the visible map extent. The `{z}/{x}/{y}` placeholders are filled by the map rendering library (MapLibre GL JS) based on the current zoom level and viewport.

The `rescale` parameter maps raw pixel values to the 0-255 display range. Different datasets use different scales — NDVI and EVI use -2000 to 10000 (raw MODIS scale factor), LST uses 13000-16000 (Kelvin × 50), LAI uses 0-70 (scale /10), and so on. The `colormap_name` parameter selects a named color ramp: `ylgn` (yellow-green) for vegetation, `ylorrd` (yellow-orange-red) for temperature, `greens` for canopy metrics, `greys` for reflectance.

### Available Layers

| Layer | Satellite Product | Resolution | Temporal | What It Measures |
|-------|------------------|-----------|----------|-----------------|
| MODIS NDVI | MOD13Q1/MYD13Q1 | 250m | 16-day | Vegetation greenness and health |
| MODIS EVI | MOD13Q1/MYD13Q1 | 250m | 16-day | Enhanced vegetation signal in dense canopy |
| MODIS LAI | MOD15A2H/MYD15A2H | 500m | 8-day | Leaf area per unit ground area |
| MODIS FPAR | MOD15A2H/MYD15A2H | 500m | 8-day | Fraction of photosynthetically active radiation absorbed |
| MODIS GPP | MOD17A2H | 500m | 8-day | Gross primary production (carbon fixation) |
| MODIS LST Day | MOD11A2/MYD11A2 | 1km | 8-day | Daytime land surface temperature |
| MODIS LST Night | MOD11A2/MYD11A2 | 1km | 8-day | Nighttime land surface temperature |
| MODIS SR Red | MOD09A1 | 500m | 8-day | Surface reflectance in red band |
| MODIS SR NIR | MOD09A1 | 500m | 8-day | Surface reflectance in near-infrared band |

---

## Data Flow

### Ingestion Flow

The ingestion pipeline runs every 8 days, triggered by EventBridge. Here is what happens from start to finish:

**1. Startup and configuration.** EventBridge launches a Fargate task with the command `--mode ingest --config /app/config/datasets.yaml`. The container loads and validates the YAML configuration, sets up structured JSON logging, and pre-loads the ward boundary GeoJSON for zonal statistics.

**2. Dataset iteration.** The pipeline iterates through each dataset defined in the configuration. Disabled datasets are skipped. For each enabled dataset, it determines the start date for the search window — either the day after the last ingested date found in S3 (incremental mode) or the lookback period from configuration (full-history mode).

**3. STAC search.** The pipeline opens a connection to the Planetary Computer STAC catalog and searches for items matching the dataset's collection name, the Kenya bounding box, and the computed date range. The Planetary Computer modifier signs each item's asset URLs in-place during the search. The search returns a list of STAC items — metadata records that include signed download URLs for each asset band.

**4. Date grouping.** MODIS data is delivered in sinusoidal tiles. Kenya spans four tiles (h21v08, h21v09, h22v08, h22v09), so a single date typically returns four STAC items. The pipeline groups all items by their date so that tiles covering the same date can be merged into a single mosaic.

**5. Skip existing dates.** Before processing, the pipeline queries S3 for all date folders already present for this dataset. Any date that already has data in S3 is skipped entirely. This prevents re-downloading data during full-history runs and makes the pipeline safely re-runnable.

**6. Per-date processing.** For each new date, and for each asset band within the dataset, the pipeline processes all tiles:

- **Download**: Each tile's asset is downloaded from the Planetary Computer. The item is re-signed immediately before download to ensure the SAS token is fresh. If the download fails with an HTTP 403, 429, or 5xx error, it retries up to 3 times with exponential backoff (5s, 10s, 20s), re-signing the item before each retry.

- **Clip**: The downloaded raster is clipped to the Kenya bounding box. If the source CRS is non-geographic (MODIS sinusoidal), the bounding box is reprojected into the source coordinate system before clipping. If the raster does not overlap the bounding box at all, it is skipped with a warning.

- **Reproject**: The clipped raster is reprojected to the target CRS (EPSG:4326) at the dataset's configured resolution. The output is written as a tiled GeoTIFF with 256×256 blocks and DEFLATE compression. Overviews are built at levels 2, 4, 8, and 16 with nearest-neighbor resampling — these overviews are what make the file a Cloud Optimized GeoTIFF, enabling efficient partial reads at different zoom levels.

- **Merge**: If multiple tiles were processed for the same date (the typical case for Kenya), they are merged into a single mosaic COG using rasterio's merge function. The merged output gets the same tiling, compression, and overview treatment.

**7. Upload.** The final COG is uploaded to S3 at the path determined by the dataset's key template (e.g., `ingested/modis-ndvi/2024-10-15/250m_16_days_NDVI.tif`).

**8. Zonal statistics.** For each uploaded COG, the pipeline computes zonal statistics against the pre-loaded ward boundaries. For each ward polygon, it calculates the count, min, max, mean, median, and standard deviation of pixel values within that polygon. The results are written as a JSON file and uploaded to S3 under the `stats/` prefix.

**9. Error handling.** Each date is processed inside a try/except block. If any step fails for a particular date (download timeout, corrupt data, merge failure), the error is logged and the pipeline moves on to the next date. This ensures that a single bad tile or expired token does not halt the entire run.

**10. Manifest.** After all datasets are processed, the pipeline writes a manifest JSON to S3 recording the run timestamp, which datasets were processed, how many items were ingested per dataset, and which S3 keys were written.

### Tile Serving Flow

When a user views the map, the following sequence occurs:

**1. Page load.** The browser loads the Prism application from Amplify. The application reads the compiled `prism.json` and `layers.json` configuration. It initializes the map centered on Marsabit County and renders the ward boundaries from the bundled GeoJSON.

**2. Layer selection.** The user opens the layer panel and selects a dataset (e.g., MODIS NDVI). The frontend reads the `dates` array for that layer and populates the date slider. It selects the most recent date by default.

**3. Tile requests.** The map library (MapLibre GL JS) determines which tiles are visible at the current zoom level and viewport, and issues parallel HTTP GET requests for each tile. Each request goes to a URL like `https://d31fsorf4vwo9f.cloudfront.net/cog/tiles/WebMercatorQuad/7/84/63?url=s3://lmr-data-cogs-dev/ingested/modis-ndvi/2026-02-26/250m_16_days_NDVI.tif&rescale=-2000,10000&colormap_name=ylgn`.

**4. CloudFront.** If the tile is in CloudFront's edge cache (within the 24-hour TTL), it is returned immediately. Otherwise, CloudFront forwards the request to the ALB origin over HTTP.

**5. ALB routing.** The ALB receives the request on port 80 and forwards it to the healthy Fargate task on port 8000.

**6. TiTiler processing.** The FastAPI application routes the request to the TiTiler COG router mounted at `/cog`. TiTiler parses the tile coordinates (z/x/y), reads the `url` query parameter, and uses GDAL's virtual filesystem (`/vsis3/`) to issue HTTP range requests directly to S3 for just the bytes needed to render that tile. It reads the appropriate overview level based on the zoom, applies the rescale range to normalize pixel values, maps them through the requested colormap, and returns a PNG image.

**7. Rendering.** The browser receives the PNG tile and composites it onto the map canvas along with the ward boundary overlay. As the user pans or zooms, new tile requests are issued and the process repeats.

**8. Date change.** When the user moves the date slider, the frontend substitutes the new date into the URL template and requests a fresh set of tiles. Previously loaded tiles are discarded and replaced.

### Inference Flow (Planned)

The planned inference flow loads a trained model from S3 and produces prediction COGs:

**1.** The serve container loads model artifacts (model file, feature scaler, training medians) from `s3://{bucket}/models/{model-name}/latest/` at startup and caches them in memory.

**2.** When triggered (via an API endpoint or scheduled run), the inference module reads the latest satellite feature data, applies median imputation for missing values using the stored training medians, scales the features using the stored StandardScaler, and runs the model's predict method.

**3.** The ward-level predictions are rasterized onto a grid using the ward boundary geometries, producing a COG that can be served as a tile layer just like the satellite data.

**4.** The prediction COG is uploaded to `s3://{bucket}/predictions/{model-name}/{date}/prediction.tif` and becomes available through the tile server immediately.

**5.** Model updates are decoupled from deployments. The team uploads new model artifacts to S3 and hits a `/reload-model` endpoint on the serve container. The container re-downloads the artifacts and hot-swaps them in memory with no restart needed.

---

## AWS Network Topology

The platform runs in the default VPC (`vpc-0c392a79120ac5b1c`) in us-east-1. Two small dedicated subnets were created for the ALB and serve Fargate tasks:

- `subnet-084b074095d2d5a57` (us-east-1a, 172.31.96.0/28)
- `subnet-0f79437f160c57ed3` (us-east-1f, 172.31.96.16/28)

These subnets are associated with a public route table (`rtb-07b7100ea5fcc5124`) that routes 0.0.0.0/0 through an Internet Gateway. An S3 VPC Gateway Endpoint is attached to this route table so that S3 traffic flows over the AWS private network rather than the public internet.

The remaining VPC subnets use the main route table which routes through a NAT Gateway. These are used by SageMaker and other services that need outbound internet access but do not need to receive inbound traffic. Keeping the ALB subnets separate ensures that changes to the LMR platform's networking do not affect other services in the account.

---

## CloudFormation Stack Structure

The infrastructure is defined as a nested CloudFormation stack. The root template (`main.yaml`) orchestrates seven child stacks:

**ECR Stack** creates the container image repository with scan-on-push and a lifecycle policy that retains the last 10 images. Its output — the repository URI — is consumed by both Fargate stacks to construct the container image reference.

**S3 Stack** creates the data bucket with versioning, EventBridge notifications, and a lifecycle rule for IA tiering. It optionally grants cross-bucket access to a SageMaker role if one is provided.

**IAM Stack** creates the three IAM roles (execution, task, EventBridge). It receives the data bucket ARN from the S3 stack to scope the task role's S3 permissions.

**Fargate Ingest Stack** creates the ECS cluster (shared with serve), a CloudWatch log group, a security group (egress-only), and the ingest task definition. It outputs the cluster ARN and task definition ARN for use by EventBridge and the serve stack.

**Fargate Serve Stack** creates the ALB, target group, HTTP listener, ECS security group (ingress from ALB only), the serve task definition with GDAL environment variables, and the ECS service. It reuses the cluster created by the ingest stack.

**EventBridge Stack** creates the scheduled rule that launches the ingest task at the configured interval.

**SageMaker Trigger Stack** (conditional, deployed only if a SageMaker pipeline ARN is provided) creates a Lambda function and EventBridge rule that trigger a SageMaker pipeline when a new manifest is uploaded to S3. This stack is planned for removal as part of the SageMaker decoupling.

---

## Key Design Decisions

**Single container, multiple modes.** Rather than maintaining separate images for ingest, serve, and inference, the platform uses one image with a CLI flag. This simplifies the build pipeline, ensures all modes share the same dependencies and configuration schema, and means there is only one ECR repository and one Dockerfile to maintain.

**Cloud Optimized GeoTIFFs.** COGs are the storage format because they support HTTP range reads — a client can request just the bytes for a specific tile at a specific zoom level without downloading the entire file. This is what makes TiTiler performant: it reads a few kilobytes from S3 per tile request rather than the full multi-megabyte raster.

**MODIS tile mosaicking.** Kenya spans four MODIS sinusoidal tiles. Rather than storing separate files per tile and complicating the serving layer, the ingest pipeline merges all tiles for the same date into a single seamless mosaic. This means the serve layer and frontend can treat each date as a single file.

**Incremental ingestion with skip-existing.** The pipeline checks S3 for dates that already have data and skips them. This makes the pipeline idempotent — you can run a full-history ingest repeatedly and it will only process dates that are missing. Combined with per-date error handling and download retries, this makes long-running ingests resilient to transient failures.

**Fargate Spot for serve.** The serve workload is stateless and restartable, making it a good fit for Spot pricing. The 70% cost reduction is significant for an always-on service. The brief interruptions during Spot reclamation are acceptable for a visualization tool.

**Dedicated ALB subnets.** The ALB and serve tasks run in their own small subnets with IGW routing, separate from the subnets used by SageMaker and other services that depend on NAT Gateway routing. This avoids a class of networking conflicts where changing route table associations for one service breaks another.

**CloudFront for HTTPS.** Rather than purchasing a domain and ACM certificate, the platform uses CloudFront's built-in `*.cloudfront.net` HTTPS certificate. This provides a free, zero-configuration HTTPS endpoint for the tile server, solving the mixed-content problem between the Amplify frontend (HTTPS) and the ALB (HTTP).

---

## File Reference

### Container Source

| Path | Purpose |
|------|---------|
| `lmr-container/src/lmr/cli.py` | CLI entrypoint — parses args, runs ingest/serve/infer |
| `lmr-container/src/lmr/config.py` | Pydantic models for YAML configuration validation |
| `lmr-container/src/lmr/common/s3.py` | Shared S3 client factory with retry configuration |
| `lmr-container/src/lmr/common/logging.py` | Structured JSON log formatter |
| `lmr-container/src/lmr/ingest/stac_client.py` | STAC catalog search via Planetary Computer |
| `lmr-container/src/lmr/ingest/cog.py` | Download, clip, reproject, merge COGs |
| `lmr-container/src/lmr/ingest/s3.py` | S3 upload, key building, manifests, date tracking |
| `lmr-container/src/lmr/ingest/zonal.py` | Ward-level zonal statistics computation |
| `lmr-container/src/lmr/serve/app.py` | FastAPI app factory with CORS, TiTiler mount, error handling |
| `lmr-container/src/lmr/serve/routes.py` | Custom API routes (health, collections, presigned URLs) |
| `lmr-container/src/lmr/serve/titiler_setup.py` | TiTiler COG router factory |
| `lmr-container/src/lmr/serve/s3.py` | Presigned URL generation, date listing, key resolution |
| `lmr-container/src/lmr/infer/predict.py` | Model loading, inference, prediction COG rasterization |

### Infrastructure

| Path | Purpose |
|------|---------|
| `lmr-container/Dockerfile` | Container image build with GDAL and uv |
| `lmr-container/pyproject.toml` | Python dependencies and CLI entrypoint definition |
| `lmr-container/config/datasets.yaml` | All platform configuration |
| `lmr-container/cloudformation/main.yaml` | Root CloudFormation stack |
| `lmr-container/cloudformation/ecr.yaml` | Container registry |
| `lmr-container/cloudformation/s3.yaml` | Data bucket |
| `lmr-container/cloudformation/iam.yaml` | IAM roles and policies |
| `lmr-container/cloudformation/fargate-ingest.yaml` | Ingest task definition and ECS cluster |
| `lmr-container/cloudformation/fargate-serve.yaml` | Serve task, ALB, and networking |
| `lmr-container/cloudformation/eventbridge.yaml` | Scheduled ingest trigger |
| `lmr-container/cloudformation/sagemaker-trigger.yaml` | Manifest-to-SageMaker pipeline trigger |

### Frontend

| Path | Purpose |
|------|---------|
| `prism-app/frontend/src/config/kenya/prism.json` | Map center, categories, default boundaries |
| `prism-app/frontend/src/config/kenya/layers.json` | Layer definitions with tile URLs, dates, legends |

---

## Appendix: Code Patterns

### S3 Key Resolution

The S3 key for any COG can be computed from three values — the dataset name, the date, and the asset name — using the template from configuration:

```
template: "{prefix}/{dataset}/{date}/{asset}.tif"
example:  "ingested/modis-ndvi/2026-02-26/250m_16_days_NDVI.tif"
```

### Tile URL Construction

A tile URL is assembled from the CloudFront domain, the TiTiler path, and three query parameters:

```
https://d31fsorf4vwo9f.cloudfront.net
  /cog/tiles/WebMercatorQuad/{z}/{x}/{y}
  ?url=s3://lmr-data-cogs-dev/ingested/modis-ndvi/2026-02-26/250m_16_days_NDVI.tif
  &rescale=-2000,10000
  &colormap_name=ylgn
```

### Retry Logic for Downloads

Downloads retry up to 3 times on HTTP 403/429/5xx with exponential backoff. The STAC item is re-signed before each attempt to refresh the SAS token:

```python
for attempt in range(max_retries):
    item = pc.sign(item)         # fresh signature each attempt
    try:
        urlretrieve(item.assets[key].href, dest)
        return dest
    except HTTPError as e:
        if e.code in (403, 429, 500, 502, 503) and attempt < max_retries - 1:
            sleep(2 ** attempt * 5)  # 5s, 10s, 20s
        else:
            raise
```

### Zonal Statistics Output Format

Each ward's statistics are stored as a JSON object with the ward identifier, name, admin level, and computed statistics:

```json
{
  "id": "KE040201",
  "name": "Turbi",
  "admin_level": 3,
  "stats": {
    "count": 4521,
    "min": 1200,
    "max": 6800,
    "mean": 3456.2,
    "median": 3400.0,
    "std": 890.1
  }
}
```

### MODIS Tile Merging

Kenya spans four MODIS sinusoidal tiles. The merge produces a single seamless COG per date:

```
h21v08 ┌────┐  h22v08 ┌────┐
       │    │         │    │        ┌──────────┐
       └────┘         └────┘  ───►  │  merged  │
h21v09 ┌────┐  h22v09 ┌────┐        │  Kenya   │
       │    │         │    │        └──────────┘
       └────┘         └────┘
```
