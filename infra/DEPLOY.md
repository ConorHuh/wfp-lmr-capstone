# LMR Platform — Deployment Guide

One-command deployment of the full LMR platform to a fresh AWS account.

## Prerequisites

| Requirement | Version | Check |
|------------|---------|-------|
| AWS CLI | v2+ | `aws sts get-caller-identity` |
| Docker | Running | `docker info` |
| Node.js | 20 (via nvm) | `node --version` |
| yarn | any | `yarn --version` |
| Python | 3.11+ | `python3 --version` |
| uv | any | `uv --version` |
| NASA Earthdata account | free | https://urs.earthdata.nasa.gov |
| Copernicus CDS account | free | https://cds.climate.copernicus.eu |

AWS credentials must be configured with sufficient permissions to create IAM roles, ECS clusters, S3 buckets, CloudFront distributions, and Amplify apps.

### NASA Earthdata Credentials

The ingest pipeline downloads 8-day MODIS ET/PET data from NASA Earthdata. Before deploying, create a Secrets Manager secret with your NASA Earthdata credentials (one-time per environment):

```bash
aws secretsmanager create-secret \
  --name lmr-earthdata-dev \
  --secret-string '{"username":"your_earthdata_username","password":"your_earthdata_password"}' \
  --region us-east-1
```

Replace `dev` with your environment name (e.g., `lmr-earthdata-staging`, `lmr-earthdata-prod`).

To create a free NASA Earthdata account, sign up at https://urs.earthdata.nasa.gov. No institution or payment required.

The Fargate ingest task definition injects `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` from this secret at runtime. The execution role has `secretsmanager:GetSecretValue` permission scoped to `lmr-earthdata-*` secrets.

### Copernicus CDS Credentials (ERA5-Land Soil Moisture)

The ingest pipeline downloads ERA5-Land monthly soil moisture data from the Copernicus Climate Data Store. Set the following environment variables for the Fargate ingest task:

- `CDSAPI_URL` — `https://cds.climate.copernicus.eu/api`
- `CDSAPI_KEY` — your personal API key from your [CDS profile page](https://cds.climate.copernicus.eu/profile)

You must also accept the ERA5-Land dataset licence at:
https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-monthly-means?tab=download#manage-licences

For local runs, set in your `.env` file or export directly:

```bash
export CDSAPI_URL="https://cds.climate.copernicus.eu/api"
export CDSAPI_KEY="your-api-key-here"
```

## Quick Start

```bash
# Full deploy (backend + frontend)
./infra/deploy-all.sh

# Backend only (faster — skips Prism build)
./infra/deploy-all.sh --skip-frontend

# Frontend only (after backend is deployed)
./infra/deploy-all.sh --skip-backend

# Skip Docker build (reuse existing image)
./infra/deploy-all.sh --skip-build

# Deploy to a different environment
./infra/deploy-all.sh --env staging

# Use a specific VPC instead of the default
./infra/deploy-all.sh --vpc-id vpc-xxx --subnet-ids subnet-aaa,subnet-bbb
```

Run `./infra/deploy-all.sh --help` for all options.

## What the Deploy Script Does

The script runs in 4 phases. On a fresh account, the full deploy takes ~15 minutes (CloudFront distribution propagation is the bottleneck).

### Phase 0 — Bootstrap

Before any infrastructure is created:

1. **Creates the CFN artifacts bucket** (`lmr-cfn-artifacts-{env}`) — needed by `aws cloudformation package` to upload nested stack templates. Idempotent.

2. **Auto-discovers the default VPC and public subnets:**
   - Finds the default VPC via `aws ec2 describe-vpcs --filters Name=isDefault,Values=true`
   - Finds public subnets using a two-step approach:
     - **Primary:** Finds route tables with `0.0.0.0/0 → igw-*` routes and gets their explicitly associated subnets
     - **Fallback:** If no explicit associations, uses subnets with `MapPublicIpOnLaunch=true` (standard default VPC setup)
   - Validates at least 2 subnets exist (ALB requires 2 AZs)
   - Override with `--vpc-id` and `--subnet-ids` if using a custom VPC

3. **Reads the inference toggle** from `backend/config/datasets.yaml`:
   ```yaml
   inference:
     enabled: true   # deploys Step Functions pipeline
     # enabled: false  # no inference infrastructure
   ```

### Phase 1 — ECR Bootstrap + Image Push

The Docker image must exist in ECR **before** CloudFormation creates ECS services (otherwise the service fails to start because there's no image to pull).

1. **Creates the ECR repository** via AWS CLI (not CloudFormation). Idempotent — no-op if the repo already exists.

2. **Builds the Docker image** from a temporary copy of `backend/`:
   - Copies `backend/` to a temp directory
   - Patches `datasets.yaml` in the copy with the environment-specific bucket name (e.g., `lmr-data-cogs-staging`)
   - **The original `datasets.yaml` is never modified**
   - Builds with `--platform linux/amd64` (required for Fargate, even from Apple Silicon)

3. **Pushes the image** to ECR (`lmr-container-{env}:latest`)

### Phase 2 — CloudFormation

Deploys the full infrastructure stack (`lmr-platform-{env}`):

```
main.yaml (root stack)
├── s3.yaml              — Data bucket (lmr-data-cogs-{env}) with EventBridge notifications
├── iam.yaml             — Task, execution, EventBridge, Step Functions, Lambda roles
├── fargate-ingest.yaml  — ECS cluster + ingest task definition (1 vCPU / 4 GB)
├── eventbridge.yaml     — Scheduled ingest trigger (every 10 days)
├── fargate-serve.yaml   — Serve task + ECS service + ALB + target group
├── cloudfront.yaml      — HTTPS distribution pointing to ALB (CORS configured)
├── amplify.yaml         — Frontend app + main branch
│
│  (only when inference.enabled = true):
├── fargate-infer.yaml   — Feature-extract (4 vCPU/16 GB) + infer (1 vCPU/4 GB) task defs
└── step-functions.yaml  — State machine + Lambda trigger + EventBridge manifest rule
```

If the stack is in `ROLLBACK_COMPLETE` state (from a prior failed attempt), the script deletes it first and creates fresh.

Parameters passed to CloudFormation:
| Parameter | Source |
|-----------|--------|
| `Environment` | `--env` flag (default: `dev`) |
| `ScheduleIntervalDays` | `--schedule-days` flag (default: `10`) |
| `ContainerImageUri` | Built from ECR repo URI + image tag |
| `VpcId` | Auto-discovered or `--vpc-id` |
| `SubnetIds` | Auto-discovered or `--subnet-ids` |
| `EnableInferencePipeline` | Read from `datasets.yaml` `inference.enabled` |

### Phase 3 — Post-Deploy

1. **Force-redeploys ECS services** to pick up the latest image
2. **Migrates model artifacts** from the SageMaker training bucket to `lmr-data-cogs-{env}/models/` (only when `inference.enabled=true`, idempotent via `aws s3 sync`)
3. **Reads CloudFront and Amplify IDs** from CloudFormation stack outputs (no hardcoded IDs)
4. **Invalidates CloudFront cache** so edge nodes pick up any backend changes

### Phase 4 — Frontend

1. Clones WFP's [prism-app](https://github.com/WFP-VAM/prism-app) at a pinned commit
2. Injects `frontend/kenya_config/` (layers.json, prism.json, admin boundaries)
3. Applies patches from `frontend/patches/` (if any)
4. Builds prism-common, then builds the frontend with `REACT_APP_COUNTRY=kenya`
5. Zips the build and uploads to Amplify via `aws amplify create-deployment`
6. Waits for Amplify deployment to succeed
7. Cleans up the temp build directory

Skip with `--skip-frontend` (saves ~10 minutes).

## Environment Isolation

Each `--env` creates a completely separate set of resources:

| Resource | dev | staging | prod |
|----------|-----|---------|------|
| CFN Stack | `lmr-platform-dev` | `lmr-platform-staging` | `lmr-platform-prod` |
| S3 Bucket | `lmr-data-cogs-dev` | `lmr-data-cogs-staging` | `lmr-data-cogs-prod` |
| ECR Repo | `lmr-container-dev` | `lmr-container-staging` | `lmr-container-prod` |
| ECS Cluster | `lmr-cluster-dev` | `lmr-cluster-staging` | `lmr-cluster-prod` |
| IAM Roles | `lmr-*-role-dev` | `lmr-*-role-staging` | `lmr-*-role-prod` |
| CloudFront | own distribution | own distribution | own distribution |
| Amplify | own app | own app | own app |

**Zero overlap.** Deploying staging never touches dev resources.

## Inference Toggle

The inference pipeline (Step Functions + feature extraction + ensemble model) is opt-in:

```yaml
# backend/config/datasets.yaml
inference:
  enabled: true    # deploys Step Functions, Lambda trigger, infer task defs
  # enabled: false  # no inference infrastructure created
```

When disabled, the container image is identical — the `infer` and `feature-extract` CLI modes exist but no AWS infrastructure triggers them. This lets WFP use the container for ingest + serve only, without paying for inference infrastructure.

When enabled, the deploy script also copies model artifacts from the SageMaker training bucket to the production bucket (`models/inference_bundle/{scheme}/`). This is idempotent and safe to re-run.

## Networking

The platform requires:
- **2+ public subnets** in different AZs (for the ALB)
- **Internet gateway route** on those subnets (for ALB inbound traffic and Fargate outbound to S3/ECR/Planetary Computer)

The deploy script auto-discovers these from the default VPC. On a fresh AWS account, this just works — the default VPC has public subnets with an IGW route.

If the default VPC has been modified (e.g., main route table changed to NAT), or if you need a custom VPC, pass explicit values:

```bash
./infra/deploy-all.sh --vpc-id vpc-xxx --subnet-ids subnet-aaa,subnet-bbb
```

The script validates the subnet count and fails with a clear error if fewer than 2 are found.

## Tearing Down

To remove all resources for an environment:

```bash
# Delete the CloudFormation stack (removes S3 bucket, IAM, ECS, ALB, CloudFront, Amplify)
aws cloudformation delete-stack --stack-name lmr-platform-{env} --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name lmr-platform-{env} --region us-east-1

# Delete the ECR repo (managed outside CloudFormation)
aws ecr delete-repository --repository-name lmr-container-{env} --force --region us-east-1

# Delete the CFN artifacts bucket
aws s3 rm s3://lmr-cfn-artifacts-{env} --recursive
aws s3 rb s3://lmr-cfn-artifacts-{env}
```

## Troubleshooting

### Stack creation fails with ECS service timeout

The ECS service couldn't pull the image or pass health checks. Check:
- Is the image in ECR? `aws ecr list-images --repository-name lmr-container-{env}`
- Are the subnets public? The ALB and ECS tasks need internet access
- Check CloudWatch logs: `/ecs/lmr-serve-{env}`

### CloudFront returns 504 Gateway Timeout

The ALB is unreachable from CloudFront. Usually means the subnets don't have an internet gateway route. Redeploy with `--subnet-ids` pointing to public subnets.

### `inference.enabled` reads as `false` unexpectedly

The deploy script uses `python3 -c "import yaml; ..."` to read the config. If the system Python doesn't have the `yaml` package, it falls back to `false`. Install PyYAML: `pip3 install pyyaml`, or use uv: `uv run python -c "import yaml"`.

### Stack is in ROLLBACK_COMPLETE

A prior deploy failed. The script detects this and auto-deletes the rolled-back stack before creating fresh. No manual intervention needed.

### Ingest fails with "NASA Earthdata login failed"

The `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment variables are missing or the Secrets Manager secret doesn't exist. Create the secret:

```bash
aws secretsmanager create-secret \
  --name lmr-earthdata-dev \
  --secret-string '{"username":"your_user","password":"your_pass"}' \
  --region us-east-1
```

For local runs, set the env vars directly:

```bash
export EARTHDATA_USERNAME="your_user"
export EARTHDATA_PASSWORD="your_pass"
```

### `--skip-build` but image doesn't exist

If you skip the build on a fresh deploy, the ECR repo will be empty. The ECS service will fail to start. Always build on first deploy; use `--skip-build` only for subsequent deploys when only CloudFormation or frontend changes are needed.
