#!/usr/bin/env bash
set -euo pipefail

# LMR Container — Build, Push, and Deploy
#
# Usage:
#   ./scripts/deploy.sh                          # deploy dev with defaults
#   ./scripts/deploy.sh --env staging             # deploy to staging
#   ./scripts/deploy.sh --skip-build              # deploy without rebuilding image
#   ./scripts/deploy.sh --env prod --tag v1.2.3   # deploy specific tag to prod

REGION="us-east-1"
ENVIRONMENT="dev"
IMAGE_TAG="latest"
SKIP_BUILD=false
STACK_NAME=""
VPC_ID="vpc-0c392a79120ac5b1c"
SUBNET_IDS="subnet-0dad0b63d1d403190"
SCHEDULE_DAYS=10

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --env ENV              Environment: dev, staging, prod (default: dev)
  --tag TAG              Docker image tag (default: latest)
  --region REGION        AWS region (default: us-east-1)
  --vpc-id ID            VPC ID (required for first deploy)
  --subnet-ids IDS       Comma-separated subnet IDs (required for first deploy)
  --schedule-days N      Ingest schedule interval in days (default: 8)
  --skip-build           Skip Docker build and push, deploy stack only
  --stack-name NAME      Override CloudFormation stack name
  -h, --help             Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENVIRONMENT="$2"; shift 2 ;;
        --tag) IMAGE_TAG="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --subnet-ids) SUBNET_IDS="$2"; shift 2 ;;
        --schedule-days) SCHEDULE_DAYS="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

STACK_NAME="${STACK_NAME:-lmr-platform-${ENVIRONMENT}}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/lmr-container-${ENVIRONMENT}"

echo "=== LMR Deploy ==="
echo "  Environment:  ${ENVIRONMENT}"
echo "  Region:       ${REGION}"
echo "  Stack:        ${STACK_NAME}"
echo "  Image tag:    ${IMAGE_TAG}"
echo "  ECR repo:     ${ECR_REPO}"
echo ""

# ── Step 1: Build Docker image locally ───────────────────────────────────────

if [[ "$SKIP_BUILD" == false ]]; then
    echo "── Building Docker image ──"
    docker build -t "lmr-container:${IMAGE_TAG}" .
else
    echo "── Skipping build (--skip-build) ──"
fi

# ── Step 2: Deploy CloudFormation (creates ECR, S3, IAM, ECS, EventBridge) ──

echo "── Deploying CloudFormation stack ──"

# Build parameter overrides
PARAMS="Environment=${ENVIRONMENT}"
PARAMS="${PARAMS} ScheduleIntervalDays=${SCHEDULE_DAYS}"
PARAMS="${PARAMS} ContainerImageTag=${IMAGE_TAG}"

if [[ -n "$VPC_ID" ]]; then
    PARAMS="${PARAMS} VpcId=${VPC_ID}"
fi
if [[ -n "$SUBNET_IDS" ]]; then
    PARAMS="${PARAMS} SubnetIds=${SUBNET_IDS}"
fi

# Package nested templates to S3
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

# ── Step 3: Push image to ECR (repo now exists from CloudFormation) ──────────

if [[ "$SKIP_BUILD" == false ]]; then
    echo "── Logging in to ECR ──"
    aws ecr get-login-password --region "${REGION}" \
        | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

    echo "── Tagging and pushing ──"
    docker tag "lmr-container:${IMAGE_TAG}" "${ECR_REPO}:${IMAGE_TAG}"
    docker push "${ECR_REPO}:${IMAGE_TAG}"

    echo "── Image pushed: ${ECR_REPO}:${IMAGE_TAG} ──"
fi

echo ""
echo "=== Deploy complete ==="
echo ""

# Print key outputs
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs" \
    --output table 2>/dev/null || true
