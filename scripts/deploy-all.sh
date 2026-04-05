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
#   ./scripts/deploy-all.sh                    # full deploy
#   ./scripts/deploy-all.sh --skip-backend     # frontend only
#   ./scripts/deploy-all.sh --skip-frontend    # backend only
#   ./scripts/deploy-all.sh --skip-build       # skip Docker build

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

# Infrastructure IDs (update these if recreating)
VPC_ID="vpc-0c392a79120ac5b1c"
SUBNET_IDS="subnet-084b074095d2d5a57,subnet-0f79437f160c57ed3"
STACK_NAME="lmr-platform-dev"
AMPLIFY_APP_ID="d3dvy50qlv6dr6"
AMPLIFY_BRANCH="main"
CLOUDFRONT_DISTRIBUTION_ID="E1GZRKL82M95B5"
CLOUDFRONT_DOMAIN="d31fsorf4vwo9f.cloudfront.net"
S3_BUCKET="lmr-data-cogs-dev"
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
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/lmr-container-${ENVIRONMENT}"

echo "============================================"
echo "  LMR Platform — Full Deployment"
echo "============================================"
echo "  Environment:   ${ENVIRONMENT}"
echo "  Region:        ${REGION}"
echo "  Stack:         ${STACK_NAME}"
echo "  Image tag:     ${IMAGE_TAG}"
echo "  Skip backend:  ${SKIP_BACKEND}"
echo "  Skip frontend: ${SKIP_FRONTEND}"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Backend — Build, Deploy CF, Push Image, Update ECS
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_BACKEND" == false ]]; then
    echo "── Step 1: Backend Deployment ──"
    cd "${REPO_ROOT}/lmr-container"

    # 1a. Build Docker image
    if [[ "$SKIP_BUILD" == false ]]; then
        echo "  Building Docker image..."
        docker build -t "lmr-container:${IMAGE_TAG}" .
    else
        echo "  Skipping Docker build (--skip-build)"
    fi

    # 1b. Deploy CloudFormation
    echo "  Deploying CloudFormation stack..."
    PARAMS="Environment=${ENVIRONMENT}"
    PARAMS="${PARAMS} ScheduleIntervalDays=${SCHEDULE_DAYS}"
    PARAMS="${PARAMS} ContainerImageTag=${IMAGE_TAG}"
    PARAMS="${PARAMS} VpcId=${VPC_ID}"
    PARAMS="${PARAMS} SubnetIds=${SUBNET_IDS}"

    PACKAGED_TEMPLATE=$(mktemp /tmp/lmr-packaged-XXXXXXXXXXXX.yaml)
    aws cloudformation package \
        --template-file cloudformation/main.yaml \
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

    # 1c. Push image to ECR
    if [[ "$SKIP_BUILD" == false ]]; then
        echo "  Pushing image to ECR..."
        aws ecr get-login-password --region "${REGION}" \
            | docker login --username AWS --password-stdin \
              "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

        docker tag "lmr-container:${IMAGE_TAG}" "${ECR_REPO}:${IMAGE_TAG}"
        docker push "${ECR_REPO}:${IMAGE_TAG}"
    fi

    # 1d. Force new deployment of ECS services
    echo "  Updating ECS services..."
    CLUSTER="lmr-cluster-${ENVIRONMENT}"

    # Update serve service (picks up new image)
    aws ecs update-service \
        --cluster "${CLUSTER}" \
        --service "lmr-serve-${ENVIRONMENT}" \
        --force-new-deployment \
        --region "${REGION}" \
        --query 'service.serviceName' --output text 2>/dev/null || echo "  (serve service not found, skipping)"

    echo "  Backend deployment complete."
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: CloudFront — Ensure Origin header forwarding for CORS
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_CLOUDFRONT" == false ]]; then
    echo "── Step 2: CloudFront CORS Configuration ──"

    # Get current config
    CF_CONFIG=$(aws cloudfront get-distribution-config --id "${CLOUDFRONT_DISTRIBUTION_ID}" --output json)
    CF_ETAG=$(echo "${CF_CONFIG}" | python3 -c "import json,sys; print(json.load(sys.stdin)['ETag'])")

    # Check if CORS is properly configured (Origin header forwarded + OPTIONS allowed)
    NEEDS_UPDATE=$(echo "${CF_CONFIG}" | python3 -c "
import json, sys
d = json.load(sys.stdin)['DistributionConfig']['DefaultCacheBehavior']
headers = d['ForwardedValues']['Headers']
methods = d['AllowedMethods']['Items']
needs = headers['Quantity'] == 0 or 'OPTIONS' not in methods
print('true' if needs else 'false')
")

    if [[ "$NEEDS_UPDATE" == "true" ]]; then
        echo "  Configuring CloudFront for CORS (Origin header + OPTIONS method)..."
        echo "${CF_CONFIG}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
config = data['DistributionConfig']
cb = config['DefaultCacheBehavior']
# Forward Origin header so backend can return CORS headers
cb['ForwardedValues']['Headers'] = {'Quantity': 1, 'Items': ['Origin']}
# Allow OPTIONS for preflight requests
cb['AllowedMethods'] = {
    'Quantity': 3,
    'Items': ['HEAD', 'GET', 'OPTIONS'],
    'CachedMethods': {'Quantity': 3, 'Items': ['HEAD', 'GET', 'OPTIONS']}
}
print(json.dumps(config))
" > /tmp/cf-config-updated.json

        aws cloudfront update-distribution \
            --id "${CLOUDFRONT_DISTRIBUTION_ID}" \
            --if-match "${CF_ETAG}" \
            --distribution-config file:///tmp/cf-config-updated.json \
            --query 'Distribution.Status' --output text

        rm -f /tmp/cf-config-updated.json

        # Invalidate cache to clear responses cached without CORS headers
        echo "  Invalidating CloudFront cache..."
        aws cloudfront create-invalidation \
            --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
            --paths "/*" \
            --query 'Invalidation.Status' --output text
    else
        echo "  CloudFront CORS already configured, skipping."
    fi

    echo "  CloudFront configuration complete."
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
    KENYA_SRC="${REPO_ROOT}/prism/kenya_config"
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
    PATCHES_DIR="${REPO_ROOT}/prism/patches"
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
echo "  Backend API:  https://${CLOUDFRONT_DOMAIN}/health"
echo "  Tile server:  https://${CLOUDFRONT_DOMAIN}/cog/tiles/..."
echo "  Frontend:     https://${AMPLIFY_BRANCH}.${AMPLIFY_APP_ID}.amplifyapp.com"
echo ""
echo "  CloudFormation stack outputs:"
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs" \
    --output table 2>/dev/null || echo "  (stack not found)"
echo ""
