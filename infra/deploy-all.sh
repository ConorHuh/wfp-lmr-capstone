#!/usr/bin/env bash
set -euo pipefail

# LMR Platform — Full Deployment Script
#
# Deploys the entire LMR platform:
#   1. Backend container (build, push to ECR, update ECS)
#   2. CloudFormation infrastructure
#   3. CloudFront HTTPS proxy (create or update)
#   4. Prism frontend (build and deploy to Amplify)
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Docker running
#   - Node.js 20 (via nvm)
#   - yarn (npm install -g yarn)
#
# Usage:
#   ./infra/deploy-all.sh                    # full deploy
#   ./infra/deploy-all.sh --skip-backend     # frontend only
#   ./infra/deploy-all.sh --skip-frontend    # backend only
#   ./infra/deploy-all.sh --skip-build       # skip Docker build

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Configuration ─────────────────────────────────────────────────────────────
REGION="us-east-1"
ENVIRONMENT="dev"
IMAGE_TAG="latest"
SKIP_BUILD=false
SKIP_BACKEND=false
SKIP_FRONTEND=false
SKIP_CLOUDFRONT=false

# VPC — auto-discovered from default VPC (override with --vpc-id / --subnet-ids)
VPC_ID=""
SUBNET_IDS=""
STACK_NAME="lmr-platform-dev"
AMPLIFY_BRANCH="main"
SCHEDULE_DAYS=10
PRISM_REPO="https://github.com/WFP-VAM/prism-app.git"
PRISM_COMMIT="6f22f3b6063ad813f3277fa312b23bb0c9bbbab0"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Deploys the full LMR platform (backend + frontend + CloudFront).

Options:
  --env ENV              Environment: dev, staging, prod (default: dev)
  --tag TAG              Docker image tag (default: latest)
  --region REGION        AWS region (default: us-east-1)
  --skip-build           Skip Docker build and push
  --skip-backend         Skip backend (container + CloudFormation)
  --skip-frontend        Skip frontend (Prism build + Amplify deploy)
  --skip-cloudfront      Skip CloudFront configuration
  --schedule-days N      Ingest schedule interval in days (default: 10)
  --vpc-id VPC_ID        Override VPC ID (default: auto-discover default VPC)
  --subnet-ids IDS       Override subnet IDs, comma-separated (default: auto-discover)
  -h, --help             Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENVIRONMENT="$2"; shift 2 ;;
        --tag) IMAGE_TAG="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        --skip-backend) SKIP_BACKEND=true; shift ;;
        --skip-frontend) SKIP_FRONTEND=true; shift ;;
        --skip-cloudfront) SKIP_CLOUDFRONT=true; shift ;;
        --schedule-days) SCHEDULE_DAYS="$2"; shift 2 ;;
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --subnet-ids) SUBNET_IDS="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/lmr-container-${ENVIRONMENT}"
STACK_NAME="lmr-platform-${ENVIRONMENT}"
S3_BUCKET="lmr-data-cogs-${ENVIRONMENT}"

# ── Bootstrap: CFN artifacts bucket (idempotent) ─────────────────────────────
aws s3 mb "s3://lmr-cfn-artifacts-${ENVIRONMENT}" --region "${REGION}" 2>/dev/null || true

# ── Auto-discover default VPC if not overridden ──────────────────────────────
if [[ -z "$VPC_ID" ]]; then
    echo "  Auto-discovering default VPC..."
    VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
        --query 'Vpcs[0].VpcId' --output text --region "${REGION}")
    if [[ "$VPC_ID" == "None" || -z "$VPC_ID" ]]; then
        echo "ERROR: No default VPC found in ${REGION}. Use --vpc-id to specify."
        exit 1
    fi
fi
if [[ -z "$SUBNET_IDS" ]]; then
    echo "  Auto-discovering public subnets for VPC ${VPC_ID}..."

    # Option A: find subnets explicitly associated with route tables that have an IGW route.
    # This correctly handles VPCs where the main route table uses a NAT gateway (private)
    # and only specific subnets are routed through the internet gateway.
    PUBLIC_SUBNETS=$(aws ec2 describe-route-tables \
        --filters "Name=vpc-id,Values=${VPC_ID}" \
                  "Name=route.destination-cidr-block,Values=0.0.0.0/0" \
                  "Name=route.gateway-id,Values=igw-*" \
        --query 'RouteTables[*].Associations[?SubnetId!=`null`].SubnetId' \
        --output text --region "${REGION}" 2>/dev/null | tr '\t' ',')

    # Option B fallback: use MapPublicIpOnLaunch flag (works for simple default VPCs)
    if [[ -z "$PUBLIC_SUBNETS" ]]; then
        echo "  No explicit IGW route table associations found, falling back to MapPublicIpOnLaunch..."
        PUBLIC_SUBNETS=$(aws ec2 describe-subnets \
            --filters "Name=vpc-id,Values=${VPC_ID}" "Name=map-public-ip-on-launch,Values=true" \
            --query 'Subnets[*].SubnetId' --output text --region "${REGION}" | tr '\t' ',')
    fi

    if [[ -z "$PUBLIC_SUBNETS" ]]; then
        echo "ERROR: No public subnets found in VPC ${VPC_ID}."
        echo "       Use --subnet-ids to specify public subnets explicitly."
        exit 1
    fi

    SUBNET_IDS="${PUBLIC_SUBNETS}"
    SUBNET_COUNT=$(echo "${SUBNET_IDS}" | tr ',' '\n' | wc -l | tr -d ' ')
    if [[ "$SUBNET_COUNT" -lt 2 ]]; then
        echo "ERROR: ALB requires at least 2 subnets in different AZs. Found ${SUBNET_COUNT}."
        echo "       Use --subnet-ids to specify at least 2 public subnets."
        exit 1
    fi
    echo "  Found ${SUBNET_COUNT} public subnet(s): ${SUBNET_IDS}"
fi

# ── Read inference toggle from datasets.yaml ─────────────────────────────────
ENABLE_INFERENCE=$(python3 -c "
import yaml
d = yaml.safe_load(open('${REPO_ROOT}/backend/config/datasets.yaml'))
print(str(d.get('inference', {}).get('enabled', False)).lower())
" 2>/dev/null || echo "false")

# ── Read serve schedule from datasets.yaml ──────────────────────────────────
SCHEDULE_CONFIG=$(python3 -c "
import yaml, json
d = yaml.safe_load(open('${REPO_ROOT}/backend/config/datasets.yaml'))
s = d.get('schedule', {})
print(json.dumps({
    'enabled': str(s.get('enabled', False)).lower(),
    'start_hour': s.get('start_hour', 8),
    'stop_hour': s.get('stop_hour', 18),
    'timezone': s.get('timezone', 'America/Los_Angeles'),
}))
" 2>/dev/null || echo '{"enabled":"false","start_hour":8,"stop_hour":18,"timezone":"America/Los_Angeles"}')

ENABLE_SCHEDULE=$(echo "${SCHEDULE_CONFIG}" | python3 -c "import sys,json; print(json.load(sys.stdin)['enabled'])")
SCHEDULE_START=$(echo "${SCHEDULE_CONFIG}" | python3 -c "import sys,json; print(json.load(sys.stdin)['start_hour'])")
SCHEDULE_STOP=$(echo "${SCHEDULE_CONFIG}" | python3 -c "import sys,json; print(json.load(sys.stdin)['stop_hour'])")
SCHEDULE_TZ=$(echo "${SCHEDULE_CONFIG}" | python3 -c "import sys,json; print(json.load(sys.stdin)['timezone'])")

echo "============================================"
echo "  LMR Platform — Full Deployment"
echo "============================================"
echo "  Environment:   ${ENVIRONMENT}"
echo "  Region:        ${REGION}"
echo "  Stack:         ${STACK_NAME}"
echo "  Image tag:     ${IMAGE_TAG}"
echo "  VPC:           ${VPC_ID}"
echo "  Subnets:       ${SUBNET_IDS}"
echo "  Inference:     ${ENABLE_INFERENCE}"
echo "  Schedule:      ${ENABLE_SCHEDULE} (${SCHEDULE_START}:00–${SCHEDULE_STOP}:00 ${SCHEDULE_TZ})"
echo "  Skip backend:  ${SKIP_BACKEND}"
echo "  Skip frontend: ${SKIP_FRONTEND}"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Backend — Build, Deploy CF, Push Image, Update ECS
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_BACKEND" == false ]]; then
    echo "── Step 1: Backend Deployment ──"
    cd "${REPO_ROOT}/backend"

    # 1a. Create a build-time copy of config with environment-specific bucket name.
    #     The original datasets.yaml is NEVER modified.
    BUILD_DIR=$(mktemp -d /tmp/lmr-build-XXXXXXXXXXXX)
    echo "  Preparing build context for environment: ${ENVIRONMENT}..."
    cp -a . "${BUILD_DIR}/"
    sed "s|lmr-data-cogs-[a-z]*|lmr-data-cogs-${ENVIRONMENT}|g" config/datasets.yaml > "${BUILD_DIR}/config/datasets.yaml"
    echo "  Build context: ${BUILD_DIR} (original config untouched)"

    # 1b. Build Docker image from the copied build context
    if [[ "$SKIP_BUILD" == false ]]; then
        echo "  Building Docker image..."
        docker build --platform linux/amd64 -t "lmr-container:${IMAGE_TAG}" "${BUILD_DIR}"
    else
        echo "  Skipping Docker build (--skip-build)"
    fi

    # ── Phase 1: ECR bootstrap + image push ────────────────────────────────
    #    ECR is managed here (not CloudFormation) because the image must exist
    #    in ECR before CFN creates ECS services that reference it.
    echo "  Phase 1: ECR bootstrap + image push"
    echo "  Ensuring ECR repository exists..."
    aws ecr create-repository \
        --repository-name "lmr-container-${ENVIRONMENT}" \
        --image-scanning-configuration scanOnPush=true \
        --region "${REGION}" 2>/dev/null \
        && echo "  Created ECR repo lmr-container-${ENVIRONMENT}" \
        || echo "  ECR repo already exists"

    if [[ "$SKIP_BUILD" == false ]]; then
        echo "  Pushing image to ECR..."
        aws ecr get-login-password --region "${REGION}" \
            | docker login --username AWS --password-stdin \
              "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

        docker tag "lmr-container:${IMAGE_TAG}" "${ECR_REPO}:${IMAGE_TAG}"
        docker push "${ECR_REPO}:${IMAGE_TAG}"
    fi

    # ── Phase 2: CloudFormation (full stack) ─────────────────────────────
    #    Image is in ECR, so ECS services can pull it during creation.

    # Clean up rolled-back stack if present (CFN can't create over ROLLBACK_COMPLETE)
    STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
        --region "${REGION}" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
    if [[ "$STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
        echo "  Deleting rolled-back stack before redeploy..."
        aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
        aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"
    fi

    echo "  Phase 2: Deploying CloudFormation stack..."
    CONTAINER_IMAGE_URI="${ECR_REPO}:${IMAGE_TAG}"
    PARAMS="Environment=${ENVIRONMENT}"
    PARAMS="${PARAMS} ScheduleIntervalDays=${SCHEDULE_DAYS}"
    PARAMS="${PARAMS} ContainerImageUri=${CONTAINER_IMAGE_URI}"
    PARAMS="${PARAMS} VpcId=${VPC_ID}"
    PARAMS="${PARAMS} SubnetIds=${SUBNET_IDS}"
    PARAMS="${PARAMS} EnableInferencePipeline=${ENABLE_INFERENCE}"
    PARAMS="${PARAMS} EnableServeSchedule=${ENABLE_SCHEDULE}"
    PARAMS="${PARAMS} ScheduleStartHour=${SCHEDULE_START}"
    PARAMS="${PARAMS} ScheduleStopHour=${SCHEDULE_STOP}"
    PARAMS="${PARAMS} ScheduleTimezone=${SCHEDULE_TZ}"

    PACKAGED_TEMPLATE=$(mktemp /tmp/lmr-packaged-XXXXXXXX).yaml
    aws cloudformation package \
        --template-file "${REPO_ROOT}/infra/cloudformation/main.yaml" \
        --s3-bucket "lmr-cfn-artifacts-${ENVIRONMENT}" \
        --output-template-file "${PACKAGED_TEMPLATE}" \
        --region "${REGION}"

    aws cloudformation deploy \
        --template-file "${PACKAGED_TEMPLATE}" \
        --stack-name "${STACK_NAME}" \
        --parameter-overrides ${PARAMS} \
        --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
        --region "${REGION}" \
        --no-fail-on-empty-changeset

    rm -f "${PACKAGED_TEMPLATE}"

    # 1e. Force new deployment of ECS services (picks up latest image)
    echo "  Updating ECS services..."
    CLUSTER="lmr-cluster-${ENVIRONMENT}"

    aws ecs update-service \
        --cluster "${CLUSTER}" \
        --service "lmr-serve-${ENVIRONMENT}" \
        --force-new-deployment \
        --region "${REGION}" \
        --query 'service.serviceName' --output text 2>/dev/null || echo "  (serve service not found, skipping)"

    # 1f. Migrate model artifacts (idempotent — only when inference enabled)
    if [[ "${ENABLE_INFERENCE}" == "true" ]]; then
        echo "  Migrating model artifacts to production bucket..."
        SM_BUCKET="amazon-sagemaker-575108933641-us-east-1-c422b90ce861"
        SM_PREFIX="dzd-ayr06tncl712p3/5t7l23o0xvt99j/shared/final_lmr_ward_results/inference_bundle"
        for scheme in biannual quadseasonal monthly; do
            aws s3 sync "s3://${SM_BUCKET}/${SM_PREFIX}/${scheme}/" \
                "s3://${S3_BUCKET}/models/inference_bundle/${scheme}/" \
                --no-progress 2>/dev/null || echo "  Warn: model migration failed for ${scheme} (check cross-account permissions)"
        done
        aws s3 cp "s3://${SM_BUCKET}/dzd-ayr06tncl712p3/5t7l23o0xvt99j/shared/geoBoundaries-KEN-ADM3.geojson" \
            "s3://${S3_BUCKET}/models/geoBoundaries-KEN-ADM3.geojson" --no-progress 2>/dev/null || true
    fi

    # 1g. Clean up build context
    rm -rf "${BUILD_DIR}"
    echo "  Backend deployment complete."
    echo ""
fi

# ── Read CFN stack outputs (CloudFront + Amplify IDs) ────────────────────────
CLOUDFRONT_DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
    --output text 2>/dev/null || echo "")
CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomain'].OutputValue" \
    --output text 2>/dev/null || echo "")
AMPLIFY_APP_ID="${AMPLIFY_APP_ID:-$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='AmplifyAppId'].OutputValue" \
    --output text 2>/dev/null || echo "")}"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: CloudFront — Now managed by CloudFormation (cloudfront.yaml)
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_CLOUDFRONT" == false && -n "$CLOUDFRONT_DISTRIBUTION_ID" ]]; then
    echo "── Step 2: CloudFront Cache Invalidation ──"
    echo "  CloudFront CORS is now managed by cloudfront.yaml."
    echo "  Invalidating cache to pick up any backend changes..."
    aws cloudfront create-invalidation \
        --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
        --paths "/*" \
        --query 'Invalidation.Status' --output text 2>/dev/null || echo "  (invalidation skipped)"
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Frontend — Build Prism and Deploy to Amplify
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_FRONTEND" == false ]]; then
    echo "── Step 3: Frontend Deployment ──"

    # 3a. Ensure Node 20 is available
    export NVM_DIR="${HOME}/.nvm"
    if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
        source "${NVM_DIR}/nvm.sh"
        nvm use 20 2>/dev/null || { echo "  Installing Node 20..."; nvm install 20; }
    else
        echo "ERROR: nvm not found. Install nvm and Node 20 first."
        echo "  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash"
        exit 1
    fi

    # 3b. Clone prism-app at pinned commit into a temp directory
    PRISM_TMPDIR=$(mktemp -d /tmp/prism-build-XXXXXXXXXXXX)
    echo "  Cloning prism-app at commit ${PRISM_COMMIT:0:8} into temp dir..."
    git clone --quiet "${PRISM_REPO}" "${PRISM_TMPDIR}"
    cd "${PRISM_TMPDIR}"
    git checkout --quiet "${PRISM_COMMIT}"

    # 3c. Inject Kenya config
    echo "  Injecting Kenya config..."
    KENYA_SRC="${REPO_ROOT}/frontend/kenya_config"
    KENYA_DST="${PRISM_TMPDIR}/frontend/src/config/kenya"

    mkdir -p "${KENYA_DST}"
    cp "${KENYA_SRC}/layers.json" "${KENYA_DST}/layers.json"
    cp "${KENYA_SRC}/prism.json" "${KENYA_DST}/prism.json"

    cat > "${KENYA_DST}/index.ts" <<'INDEXEOF'
import appConfig from './prism.json';
import rawLayers from './layers.json';

const translation = {
  en: { 'Admin 1': 'Province', 'Admin 2': 'District', 'Admin 3': 'Ward' },
};
const rawTables = {};
const rawReports = {};

export default {
  appConfig,
  rawLayers,
  rawReports,
  rawTables,
  translation,
  defaultBoundariesFile: 'ken_bnd_adm3_WFP.json',
};
INDEXEOF

    # Register Kenya in config/index.ts
    CONFIG_INDEX="${PRISM_TMPDIR}/frontend/src/config/index.ts"
    sed -i '' "s/import jordan from '.\/jordan';/import jordan from '.\/jordan';\nimport kenya from '.\/kenya';/" "${CONFIG_INDEX}"
    sed -i '' "s/  jordan,/  jordan,\n  kenya,/" "${CONFIG_INDEX}"

    # Copy boundary file to public data
    mkdir -p "${PRISM_TMPDIR}/frontend/public/data/kenya"
    cp "${KENYA_SRC}/admin_boundaries.geojson" \
       "${PRISM_TMPDIR}/frontend/public/data/kenya/ken_bnd_adm3_WFP.json"

    # 3d. Apply patches (e.g. hyphenated date format support)
    PATCHES_DIR="${REPO_ROOT}/frontend/patches"
    if [[ -d "${PATCHES_DIR}" ]]; then
        echo "  Applying patches..."
        for patch in "${PATCHES_DIR}"/*.patch; do
            [[ -f "$patch" ]] || continue
            echo "    $(basename "$patch")"
            git apply "$patch"
        done
    fi

    # 3e. Build prism-common
    echo "  Building prism-common..."
    cd "${PRISM_TMPDIR}/common"
    yarn install --network-timeout 600000 2>/dev/null
    yarn build 2>/dev/null

    # 3f. Build frontend
    echo "  Installing frontend dependencies..."
    cd "${PRISM_TMPDIR}/frontend"
    yarn install --network-timeout 600000 2>/dev/null

    echo "  Building frontend for Kenya..."
    REACT_APP_COUNTRY=kenya npx cross-env vite build 2>&1 | tail -5

    # 3g. Deploy to Amplify
    echo "  Creating Amplify deployment..."
    cd "${PRISM_TMPDIR}/frontend/build"
    zip -qr /tmp/prism-build.zip .

    DEPLOY_RESPONSE=$(aws amplify create-deployment \
        --app-id "${AMPLIFY_APP_ID}" \
        --branch-name "${AMPLIFY_BRANCH}" \
        --output json)

    JOB_ID=$(echo "${DEPLOY_RESPONSE}" | python3 -c "import json,sys; print(json.load(sys.stdin)['jobId'])")
    UPLOAD_URL=$(echo "${DEPLOY_RESPONSE}" | python3 -c "import json,sys; print(json.load(sys.stdin)['zipUploadUrl'])")

    echo "  Uploading build (job ${JOB_ID})..."
    curl -s -o /dev/null -T /tmp/prism-build.zip -H "Content-Type: application/zip" "${UPLOAD_URL}"

    aws amplify start-deployment \
        --app-id "${AMPLIFY_APP_ID}" \
        --branch-name "${AMPLIFY_BRANCH}" \
        --job-id "${JOB_ID}" \
        --query 'jobSummary.status' --output text

    # Wait for deployment
    echo "  Waiting for Amplify deployment..."
    for i in {1..20}; do
        sleep 5
        STATUS=$(aws amplify get-job \
            --app-id "${AMPLIFY_APP_ID}" \
            --branch-name "${AMPLIFY_BRANCH}" \
            --job-id "${JOB_ID}" \
            --query 'job.summary.status' --output text)
        if [[ "$STATUS" == "SUCCEED" ]]; then
            echo "  Amplify deployment succeeded."
            break
        elif [[ "$STATUS" == "FAILED" ]]; then
            echo "ERROR: Amplify deployment failed!"
            exit 1
        fi
    done

    # 3h. Clean up temp directory
    echo "  Cleaning up temp build directory..."
    rm -rf "${PRISM_TMPDIR}"
    rm -f /tmp/prism-build.zip

    echo "  Frontend deployment complete."
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════

echo "============================================"
echo "  Deployment Complete"
echo "============================================"
echo ""
echo "  Backend API:  https://${CLOUDFRONT_DOMAIN:-<pending>}/health"
echo "  Tile server:  https://${CLOUDFRONT_DOMAIN:-<pending>}/cog/tiles/..."
echo "  Frontend:     https://${AMPLIFY_BRANCH}.${AMPLIFY_APP_ID:-<pending>}.amplifyapp.com"
echo "  Inference:    ${ENABLE_INFERENCE}"
echo ""
echo "  CloudFormation stack outputs:"
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs" \
    --output table 2>/dev/null || echo "  (stack not found)"
echo ""
